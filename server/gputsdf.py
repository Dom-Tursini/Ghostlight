"""
Dense TSDF volume on the GPU, in CUDA via CuPy.

Why this and not Open3D: Open3D's CUDA wheels are Linux only, and building it
for Windows means Visual Studio, CMake and hours. CuPy compiles RawKernels
against the installed CUDA toolkit and gets us the 3090 in one pip install.

Why dense and not hashed blocks: hashing exists so you can scan a whole
building without allocating empty space. Scanning an object into a bounded
volume, the whole grid fits comfortably. A 1.5 m cube at 5 mm is 300^3 voxels,
which is 27 M voxels, 216 MB for TSDF plus weight. On 24 GB that is nothing,
and dense indexing removes an entire class of complexity.

The pipeline is KinectFusion's, in the order it does it:

    bilateral filter -> back-project -> ICP against a raycast of the model
    -> integrate at the solved pose

The important part, and the thing plain point accumulation cannot do, is that
integrate keeps a running weighted average per voxel. Seeing a surface twice
makes it *better*, not thicker.

Poses are camera-to-world 4x4 matrices in the CV convention (X right, Y down,
Z forward), matching the depth camera. Conversion to GL happens on the wire.
"""

from __future__ import annotations

import math

import cupy as cp
import numpy as np

# --- kernels ----------------------------------------------------------------

_SRC = r'''
extern "C" {

/* Edge-preserving denoise. Kinect depth is noisy enough that integrating it
   raw thickens every surface; a box blur would destroy the depth
   discontinuities that ICP relies on. */
__global__ void bilateral(const unsigned short* src, unsigned short* dst,
                          int W, int H, float sigma_s, float sigma_r, int rad)
{
    int x = blockIdx.x * blockDim.x + threadIdx.x;
    int y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= W || y >= H) return;

    int idx = y * W + x;
    unsigned short c = src[idx];
    if (c == 0) { dst[idx] = 0; return; }

    float centre = (float)c;
    float sum = 0.0f, wsum = 0.0f;
    float inv_s = -0.5f / (sigma_s * sigma_s);
    float inv_r = -0.5f / (sigma_r * sigma_r);

    for (int dy = -rad; dy <= rad; ++dy) {
        int yy = y + dy;
        if (yy < 0 || yy >= H) continue;
        for (int dx = -rad; dx <= rad; ++dx) {
            int xx = x + dx;
            if (xx < 0 || xx >= W) continue;
            unsigned short s = src[yy * W + xx];
            if (s == 0) continue;
            float v = (float)s;
            float dr = v - centre;
            float w = __expf((dx*dx + dy*dy) * inv_s + dr*dr * inv_r);
            sum += w * v;
            wsum += w;
        }
    }
    dst[idx] = (unsigned short)(wsum > 0.0f ? (sum / wsum) : centre);
}

/* One thread per voxel. Project the voxel centre into the camera, compare its
   depth to the measurement, and fold the signed distance into the running
   average. */
__global__ void integrate(float* tsdf, float* weight,
                          const unsigned short* depth,
                          int NX, int NY, int NZ,
                          float ox, float oy, float oz, float voxel,
                          const float* w2c,          /* world -> camera, 4x4 */
                          float fx, float fy, float cx, float cy,
                          int W, int H,
                          float trunc, float dmin, float dmax, float wmax)
{
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    int total = NX * NY * NZ;
    if (i >= total) return;

    int vz = i / (NX * NY);
    int rem = i - vz * NX * NY;
    int vy = rem / NX;
    int vx = rem - vy * NX;

    float px = ox + (vx + 0.5f) * voxel;
    float py = oy + (vy + 0.5f) * voxel;
    float pz = oz + (vz + 0.5f) * voxel;

    float X = w2c[0]*px + w2c[1]*py + w2c[2]*pz  + w2c[3];
    float Y = w2c[4]*px + w2c[5]*py + w2c[6]*pz  + w2c[7];
    float Z = w2c[8]*px + w2c[9]*py + w2c[10]*pz + w2c[11];
    if (Z <= 0.0f) return;

    int u = (int)(fx * X / Z + cx + 0.5f);
    int v = (int)(fy * Y / Z + cy + 0.5f);
    if (u < 0 || u >= W || v < 0 || v >= H) return;

    unsigned short raw = depth[v * W + u];
    if (raw == 0) return;
    float d = raw * 0.001f;
    if (d < dmin || d > dmax) return;

    float sdf = d - Z;
    if (sdf < -trunc) return;                 /* behind the surface: unseen */

    float t = sdf / trunc;
    if (t > 1.0f) t = 1.0f;

    float w_old = weight[i];
    float t_old = tsdf[i];
    float w_new = w_old + 1.0f;
    if (w_new > wmax) w_new = wmax;
    tsdf[i]   = (t_old * w_old + t) / w_new;
    weight[i] = w_new;
}

/* Same as integrate, but also folds the colour camera's pixel into a running
   average per voxel. Depth and colour sit side by side on the sensor, so the
   mapping is a fit against the SDK's own per-pixel mapper (mean 2.1 px):

       u_c = CU_A*u + CU_B/z + CU_C
       v_c = CV_A*v          + CV_C

   Colour is only written near the zero crossing; away from the surface it is
   meaningless and would just dilute the average. */
__global__ void integrate_rgb(float* tsdf, float* weight, float* colour,
                              const unsigned short* depth,
                              const unsigned char* bgra,
                              int NX, int NY, int NZ,
                              float ox, float oy, float oz, float voxel,
                              const float* w2c,
                              float fx, float fy, float cx, float cy,
                              int W, int H,
                              float trunc, float dmin, float dmax, float wmax,
                              float cu_a, float cu_b, float cu_c,
                              float cv_a, float cv_c)
{
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    int total = NX * NY * NZ;
    if (i >= total) return;

    int vz = i / (NX * NY);
    int rem = i - vz * NX * NY;
    int vy = rem / NX;
    int vx = rem - vy * NX;

    float px = ox + (vx + 0.5f) * voxel;
    float py = oy + (vy + 0.5f) * voxel;
    float pz = oz + (vz + 0.5f) * voxel;

    float X = w2c[0]*px + w2c[1]*py + w2c[2]*pz  + w2c[3];
    float Y = w2c[4]*px + w2c[5]*py + w2c[6]*pz  + w2c[7];
    float Z = w2c[8]*px + w2c[9]*py + w2c[10]*pz + w2c[11];
    if (Z <= 0.0f) return;

    int u = (int)(fx * X / Z + cx + 0.5f);
    int v = (int)(fy * Y / Z + cy + 0.5f);
    if (u < 0 || u >= W || v < 0 || v >= H) return;

    unsigned short raw = depth[v * W + u];
    if (raw == 0) return;
    float d = raw * 0.001f;
    if (d < dmin || d > dmax) return;

    float sdf = d - Z;
    if (sdf < -trunc) return;
    float t = sdf / trunc;
    if (t > 1.0f) t = 1.0f;

    float w_old = weight[i];
    float t_old = tsdf[i];
    float w_new = w_old + 1.0f;
    if (w_new > wmax) w_new = wmax;
    tsdf[i]   = (t_old * w_old + t) / w_new;
    weight[i] = w_new;

    if (fabsf(t) > 0.7f) return;                  /* not near the surface */

    int cu = (int)(cu_a * u + cu_b / d + cu_c + 0.5f);
    int cv = (int)(cv_a * v + cv_c + 0.5f);
    if (cu < 0 || cu >= W || cv < 0 || cv >= H) return;

    int ci = (cv * W + cu) * 4;                   /* BGRA */
    float b = bgra[ci + 0], g = bgra[ci + 1], r = bgra[ci + 2];

    colour[i*3+0] = (colour[i*3+0] * w_old + r) / w_new;
    colour[i*3+1] = (colour[i*3+1] * w_old + g) / w_new;
    colour[i*3+2] = (colour[i*3+2] * w_old + b) / w_new;
}

/* Central-difference gradient of the TSDF at each surface voxel. Gives the
   surface normal for free, which lets the client shade the preview points into
   a solid-looking surface without meshing anything. */
__global__ void surface_normals(const float* tsdf, const float* weight,
                                const long long* idx, int n,
                                int NX, int NY, int NZ, float* out)
{
    int k = blockIdx.x * blockDim.x + threadIdx.x;
    if (k >= n) return;
    long long i = idx[k];
    int nxny = NX * NY;
    int z = (int)(i / nxny);
    int rem = (int)(i - (long long)z * nxny);
    int y = rem / NX;
    int x = rem - y * NX;

    out[k*3+0] = out[k*3+1] = out[k*3+2] = 0.0f;
    if (x < 1 || y < 1 || z < 1 || x+1 >= NX || y+1 >= NY || z+1 >= NZ) return;

    #define AT(xx,yy,zz) ((long long)(zz)*nxny + (long long)(yy)*NX + (xx))
    long long ipx = AT(x+1,y,z), imx = AT(x-1,y,z);
    long long ipy = AT(x,y+1,z), imy = AT(x,y-1,z);
    long long ipz = AT(x,y,z+1), imz = AT(x,y,z-1);
    #undef AT
    if (weight[ipx]<=0||weight[imx]<=0||weight[ipy]<=0||
        weight[imy]<=0||weight[ipz]<=0||weight[imz]<=0) return;

    float gx = tsdf[ipx]-tsdf[imx];
    float gy = tsdf[ipy]-tsdf[imy];
    float gz = tsdf[ipz]-tsdf[imz];
    float l = sqrtf(gx*gx+gy*gy+gz*gz);
    if (l < 1e-8f) return;
    out[k*3+0] = gx/l; out[k*3+1] = gy/l; out[k*3+2] = gz/l;
}

__device__ __forceinline__ bool trilinear(const float* tsdf, const float* weight,
                                          int NX, int NY, int NZ,
                                          float gx, float gy, float gz, float* out)
{
    int x0 = (int)floorf(gx), y0 = (int)floorf(gy), z0 = (int)floorf(gz);
    if (x0 < 0 || y0 < 0 || z0 < 0 || x0+1 >= NX || y0+1 >= NY || z0+1 >= NZ) return false;
    float a = gx - x0, b = gy - y0, c = gz - z0;
    float acc = 0.0f;
    for (int k = 0; k < 8; ++k) {
        int dx = k & 1, dy = (k >> 1) & 1, dz = (k >> 2) & 1;
        int idx = (z0+dz)*NX*NY + (y0+dy)*NX + (x0+dx);
        if (weight[idx] <= 0.0f) return false;
        float wgt = (dx ? a : 1-a) * (dy ? b : 1-b) * (dz ? c : 1-c);
        acc += wgt * tsdf[idx];
    }
    *out = acc;
    return true;
}

/* One thread per pixel. March until the TSDF changes sign, then refine and
   read the gradient for a normal. Output is in camera space so ICP can use it
   directly. */
__global__ void raycast(const float* tsdf, const float* weight,
                        int NX, int NY, int NZ,
                        float ox, float oy, float oz, float voxel,
                        const float* c2w,          /* camera -> world, 4x4 */
                        float fx, float fy, float cx, float cy,
                        int W, int H,
                        float trunc, float dmin, float dmax,
                        float* vmap, float* nmap)
{
    int x = blockIdx.x * blockDim.x + threadIdx.x;
    int y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= W || y >= H) return;
    int pix = y * W + x;
    vmap[pix*3+0] = nanf(""); vmap[pix*3+1] = nanf(""); vmap[pix*3+2] = nanf("");
    nmap[pix*3+0] = nanf(""); nmap[pix*3+1] = nanf(""); nmap[pix*3+2] = nanf("");

    float dx = (x - cx) / fx, dy = (y - cy) / fy, dz = 1.0f;
    float rl = sqrtf(dx*dx + dy*dy + dz*dz);
    dx /= rl; dy /= rl; dz /= rl;

    float wx = c2w[0]*dx + c2w[1]*dy + c2w[2]*dz;
    float wy = c2w[4]*dx + c2w[5]*dy + c2w[6]*dz;
    float wz = c2w[8]*dx + c2w[9]*dy + c2w[10]*dz;
    float px = c2w[3], py = c2w[7], pz = c2w[11];

    float step = trunc * 0.5f;
    float prev = 0.0f; bool have_prev = false; float prev_t = dmin;

    for (float t = dmin; t < dmax; t += step) {
        float sx = px + wx*t, sy = py + wy*t, sz = pz + wz*t;
        float gx = (sx - ox)/voxel - 0.5f;
        float gy = (sy - oy)/voxel - 0.5f;
        float gz = (sz - oz)/voxel - 0.5f;
        float val;
        if (!trilinear(tsdf, weight, NX, NY, NZ, gx, gy, gz, &val)) { have_prev = false; continue; }

        if (have_prev && prev > 0.0f && val <= 0.0f) {
            float tt = prev_t + step * (prev / (prev - val));      /* zero crossing */
            float hx = px + wx*tt, hy = py + wy*tt, hz = pz + wz*tt;

            float g0, g1, e = voxel;
            float nx_, ny_, nz_;
            float bx = (hx-ox)/voxel - 0.5f, by = (hy-oy)/voxel - 0.5f, bz = (hz-oz)/voxel - 0.5f;
            if (!trilinear(tsdf,weight,NX,NY,NZ,bx+1,by,bz,&g1)) return;
            if (!trilinear(tsdf,weight,NX,NY,NZ,bx-1,by,bz,&g0)) return;
            nx_ = g1-g0;
            if (!trilinear(tsdf,weight,NX,NY,NZ,bx,by+1,bz,&g1)) return;
            if (!trilinear(tsdf,weight,NX,NY,NZ,bx,by-1,bz,&g0)) return;
            ny_ = g1-g0;
            if (!trilinear(tsdf,weight,NX,NY,NZ,bx,by,bz+1,&g1)) return;
            if (!trilinear(tsdf,weight,NX,NY,NZ,bx,by,bz-1,&g0)) return;
            nz_ = g1-g0;
            float nl = sqrtf(nx_*nx_ + ny_*ny_ + nz_*nz_);
            if (nl < 1e-8f) return;
            nx_/=nl; ny_/=nl; nz_/=nl;

            vmap[pix*3+0]=hx; vmap[pix*3+1]=hy; vmap[pix*3+2]=hz;
            nmap[pix*3+0]=nx_; nmap[pix*3+1]=ny_; nmap[pix*3+2]=nz_;
            return;
        }
        prev = val; prev_t = t; have_prev = true;
    }
}

/* Projective data association, one thread per pixel of the new frame. Builds
   the linearised point-to-plane rows; the 6x6 normal equations are formed by
   CuPy afterwards, which is less code than a hand-rolled reduction and just
   as fast at this size. */
__global__ void icp_rows(const unsigned short* depth,
                         const float* vmap, const float* nmap,   /* model, world */
                         const float* c2w,                       /* current guess */
                         const float* w2c_prev,                  /* model camera */
                         float fx, float fy, float cx, float cy,
                         int W, int H, float dmin, float dmax,
                         float dist_thresh, float normal_thresh,
                         float ox, float oy, float oz, float vsize,
                         float* A, float* b,
                         unsigned char* valid, unsigned char* inbox)
{
    int x = blockIdx.x * blockDim.x + threadIdx.x;
    int y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= W || y >= H) return;
    int pix = y * W + x;
    valid[pix] = 0;
    inbox[pix] = 0;

    unsigned short raw = depth[pix];
    if (raw == 0) return;
    float d = raw * 0.001f;
    if (d < dmin || d > dmax) return;

    /* new frame point, camera -> world with the current pose estimate */
    float X = (x - cx) * d / fx, Y = (y - cy) * d / fy, Z = d;
    float sx = c2w[0]*X + c2w[1]*Y + c2w[2]*Z  + c2w[3];
    float sy = c2w[4]*X + c2w[5]*Y + c2w[6]*Z  + c2w[7];
    float sz = c2w[8]*X + c2w[9]*Y + c2w[10]*Z + c2w[11];

    /* Only the scan volume drives tracking. Without this the room dominates:
       on a turntable the walls are static and the object is not, so ICP locks
       onto the walls, reports no motion, and the object smears. */
    if (sx < ox || sx > ox + vsize ||
        sy < oy || sy > oy + vsize ||
        sz < oz || sz > oz + vsize) return;
    inbox[pix] = 1;

    /* project into the model camera to find its correspondence */
    float mx = w2c_prev[0]*sx + w2c_prev[1]*sy + w2c_prev[2]*sz  + w2c_prev[3];
    float my = w2c_prev[4]*sx + w2c_prev[5]*sy + w2c_prev[6]*sz  + w2c_prev[7];
    float mz = w2c_prev[8]*sx + w2c_prev[9]*sy + w2c_prev[10]*sz + w2c_prev[11];
    if (mz <= 0.0f) return;
    int u = (int)(fx * mx / mz + cx + 0.5f);
    int v = (int)(fy * my / mz + cy + 0.5f);
    if (u < 0 || u >= W || v < 0 || v >= H) return;

    int mi = v * W + u;
    float tx = vmap[mi*3+0], ty = vmap[mi*3+1], tz = vmap[mi*3+2];
    if (isnan(tx)) return;
    float nx_ = nmap[mi*3+0], ny_ = nmap[mi*3+1], nz_ = nmap[mi*3+2];
    if (isnan(nx_)) return;

    float ex = sx-tx, ey = sy-ty, ez = sz-tz;
    if (ex*ex + ey*ey + ez*ez > dist_thresh*dist_thresh) return;

    float dot = ex*nx_ + ey*ny_ + ez*nz_;

    /* rows of [ p x n | n ] for the 6-DoF increment */
    A[pix*6+0] = sy*nz_ - sz*ny_;
    A[pix*6+1] = sz*nx_ - sx*nz_;
    A[pix*6+2] = sx*ny_ - sy*nx_;
    A[pix*6+3] = nx_;
    A[pix*6+4] = ny_;
    A[pix*6+5] = nz_;
    b[pix] = -dot;
    valid[pix] = 1;
}

}
'''

# Depth -> colour pixel mapping, least-squares fitted against
# NuiImageGetColorPixelCoordinatesFromDepthPixelAtResolution over an 840 point
# grid. Mean residual 2.1 px, max 4.5 px.
CU_A, CU_B, CU_C = 0.924115, 11.917571, 16.4471
CV_A, CV_C = 0.920909, 30.2714

_mod = cp.RawModule(code=_SRC, options=('-std=c++11',))
_k_bilateral = _mod.get_function('bilateral')
_k_integrate = _mod.get_function('integrate')
_k_integrate_rgb = _mod.get_function('integrate_rgb')
_k_raycast = _mod.get_function('raycast')
_k_icp = _mod.get_function('icp_rows')
_k_normals = _mod.get_function('surface_normals')


def _se3(xi: np.ndarray) -> np.ndarray:
    """Small-angle exponential map. Valid because ICP increments are tiny."""
    T = np.eye(4, dtype=np.float32)
    wx, wy, wz, tx, ty, tz = [float(v) for v in xi]
    T[:3, :3] = np.array([[1, -wz, wy], [wz, 1, -wx], [-wy, wx, 1]], dtype=np.float32)
    # re-orthonormalise so repeated increments cannot drift into a shear
    u, _, vt = np.linalg.svd(T[:3, :3])
    T[:3, :3] = (u @ vt).astype(np.float32)
    T[:3, 3] = (tx, ty, tz)
    return T


# Bytes of VRAM per voxel. The volume itself is a float32 distance and a float32
# weight; colour adds three more float32 channels, and it is allocated lazily on
# the first coloured frame, which is why a grid that fitted a moment ago can stop
# fitting the instant marker tracking turns colour on.
VOXEL_STEP_MM = 0.5      # the step the voxel sliders move in

BYTES_GEOM = 8
BYTES_COLOUR = 12

# Fraction of free VRAM a volume may claim. The rest goes on the per-frame
# buffers, the raycast, marching cubes (which briefly holds a vertex array
# proportional to the surface), and whatever the display driver wants for the
# browser's own WebGL context.
VRAM_SHARE = 0.75


def host_free():
    """Free system memory in bytes, or None if it cannot be read."""
    try:
        import psutil
        return int(psutil.virtual_memory().available)
    except Exception:
        return None


def _mesh_host_bytes(n_voxels):
    """Roughly what meshing will ask the host for.

    One float32 copy of the volume, plus as much again for what skimage builds
    while it walks the grid. That second term is an estimate rather than a
    measurement, so the check is a guard against the obvious failure, not a
    guarantee.
    """
    return int(n_voxels * 4 * 2.0)


def _coarsest_mm(size_m, free_bytes):
    """The finest voxel whose grid should still mesh in the memory available,
    rounded up to a size the sliders can reach."""
    max_n = max(8, int((free_bytes / 8.0) ** (1.0 / 3.0)))
    return math.ceil(size_m / max_n * 1000 / VOXEL_STEP_MM) * VOXEL_STEP_MM


def release():
    """Hand freed blocks back to the driver.

    CuPy pools its allocations, so dropping the last reference to a volume
    returns the memory to the pool and not to the device. Without this the card
    still reports it as in use, the next volume is measured against a budget
    that has already been spent, and a rebuild at a different voxel size is
    refused for want of memory nothing is actually using.
    """
    try:
        cp.get_default_memory_pool().free_all_blocks()
        cp.get_default_pinned_memory_pool().free_all_blocks()
    except Exception:
        pass


def device_info() -> dict:
    """Which GPU this is actually running on.

    The status bar used to carry a hardcoded card name, which was wrong for
    everyone else and, as it turned out, wrong about the CUDA version on the
    machine it was copied from.
    """
    try:
        dev = cp.cuda.Device()
        p = cp.cuda.runtime.getDeviceProperties(dev.id)
        name = p['name']
        if isinstance(name, bytes):
            name = name.decode('utf-8', 'replace')
        v = cp.cuda.runtime.runtimeGetVersion()
        free, total = vram()
        return {
            'name': name.replace('NVIDIA ', '').strip(),
            'arch': 'sm_%d%d' % (p['major'], p['minor']),
            'cuda': '%d.%d' % (v // 1000, (v % 1000) // 10),
            'vram_gb': round((total or 0) / 1e9, 1),
            'free_gb': round((free or 0) / 1e9, 1),
            # What a volume may actually claim, by the same rule the constructor
            # enforces. Published so the interface can warn against the real
            # card instead of a guess: a fixed ceiling is wrong on every machine
            # except the one it was written on, waving through requests a small
            # card cannot meet and nagging about ones a large card can.
            'budget_gb': round((free or 0) * VRAM_SHARE / 1e9, 1),
            'bytes_per_voxel': BYTES_GEOM + BYTES_COLOUR,
        }
    except Exception:
        return {}


def vram():
    """(free, total) device bytes, or (None, None) if the driver will not say."""
    try:
        free, total = cp.cuda.runtime.memGetInfo()
        return int(free), int(total)
    except Exception:
        return None, None


def plan(size, voxel, colour=True):
    """What a volume would cost, and whether it fits.

    Separated from the constructor so callers can ask before committing. There
    is no recovering from getting this wrong at allocation time: overcommitting
    the device does not raise, it corrupts, and the next kernel dies with an
    illegal memory access that takes the process with it.
    """
    n = max(1, int(round(size / voxel)))
    per = BYTES_GEOM + (BYTES_COLOUR if colour else 0)
    need = (n ** 3) * per
    free, total = vram()
    budget = int(free * VRAM_SHARE) if free else None
    fits = budget is None or need <= budget
    # The finest grid that would fit, so the message can suggest one.
    finest = None
    if budget:
        max_n = int((budget / per) ** (1.0 / 3.0))
        if max_n >= 8:
            finest = size / max_n
    return {'grid': n, 'bytes': need, 'free': free, 'total': total,
            'fits': fits, 'finest_voxel': finest}


class Volume:
    def __init__(self, size=1.5, voxel=0.005, centre_z=1.2,
                 centre_x=0.0, centre_y=0.0,
                 dmin=0.5, dmax=2.5, fx=571.26, fy=571.26, cx=320.0, cy=240.0,
                 width=640, height=480, wmax=64.0, reserve_colour=True):
        self.W, self.H = width, height
        self.fx, self.fy, self.cx, self.cy = fx, fy, cx, cy
        self.voxel = float(voxel)
        self.trunc = float(voxel * 5)
        # Clip to the box, with a margin. Letting these be set independently
        # meant a far clip could sit in front of the scan volume and silently
        # discard every pixel of the subject before integration ever saw it.
        near_face = self.origin[2] if hasattr(self, 'origin') else centre_z - size / 2
        self.dmin = max(0.4, min(float(dmin), centre_z - size * 0.75))
        self.dmax = max(float(dmax), centre_z + size * 0.75)
        self.wmax = float(wmax)

        n = int(round(size / voxel))

        # Refuse before allocating rather than crash during it. CUDA does not
        # fail politely here: the allocation succeeds, the first kernel to touch
        # past the end dies with CUDA_ERROR_ILLEGAL_ADDRESS, and the context is
        # unrecoverable, so the service exits and takes the in-progress
        # recording with it.
        p = plan(size, voxel, colour=reserve_colour)
        if not p['fits']:
            msg = ('%d³ voxels needs %.1f GB of VRAM. Only %.1f GB is free and '
                   'the volume may claim %.0f%% of that, leaving room for '
                   'meshing and the display.'
                   % (p['grid'], p['bytes'] / 1e9, (p['free'] or 0) / 1e9,
                      VRAM_SHARE * 100))
            if p['finest_voxel']:
                # Up to the next step the slider can actually reach, so the
                # number named is one that fits.
                step = math.ceil(p['finest_voxel'] * 1000 / VOXEL_STEP_MM) * VOXEL_STEP_MM
                msg += (' Use a voxel size of %.1f mm or larger at this volume, '
                        'or shrink the volume.' % step)
            raise MemoryError(msg)

        self.NX = self.NY = self.NZ = n
        # Positionable, not just centred on the optical axis. A subject on a
        # turntable sits below the axis with the table right underneath it, and
        # if the table falls inside the volume it dominates tracking: it is
        # static while the subject rotates, so ICP reports no motion and the
        # subject smears into a solid of revolution.
        self.origin = (centre_x - size / 2.0,
                       centre_y - size / 2.0,
                       centre_z - size / 2.0)
        # Box corners can sit further than the centre, so widen to cover them.
        far_corner = centre_z + size * 0.87
        self.dmin = max(0.4, min(self.dmin, centre_z - size * 0.87))
        self.dmax = max(self.dmax, far_corner)

        self.tsdf = cp.ones((n * n * n,), dtype=cp.float32)
        self.weight = cp.zeros((n * n * n,), dtype=cp.float32)
        # Colour is optional: allocated on first coloured frame so a depth-only
        # session does not pay three extra float channels for nothing.
        self.colour = None
        self._cbuf = None

        self._vmap = cp.empty((self.H * self.W * 3,), dtype=cp.float32)
        self._nmap = cp.empty((self.H * self.W * 3,), dtype=cp.float32)
        self._A = cp.zeros((self.H * self.W * 6,), dtype=cp.float32)
        self._b = cp.zeros((self.H * self.W,), dtype=cp.float32)
        self._valid = cp.zeros((self.H * self.W,), dtype=cp.uint8)
        self._inbox = cp.zeros((self.H * self.W,), dtype=cp.uint8)
        self._dbuf = cp.empty((self.H * self.W,), dtype=cp.uint16)
        self.surface_count = 0
        self.inbox = 0

    # -- helpers -------------------------------------------------------------

    @property
    def bytes(self):
        n = int(self.tsdf.nbytes + self.weight.nbytes)
        if self.colour is not None:
            n += int(self.colour.nbytes)
        return n

    def reset(self):
        self.tsdf.fill(1.0)
        self.weight.fill(0.0)
        if self.colour is not None:
            self.colour.fill(0.0)
        self.surface_count = 0

    def _ensure_colour(self):
        if self.colour is None:
            n = self.NX * self.NY * self.NZ
            self.colour = cp.zeros((n * 3,), dtype=cp.float32)
            self._cbuf = cp.empty((self.H * self.W * 4,), dtype=cp.uint8)

    def denoise(self, depth_mm: np.ndarray) -> cp.ndarray:
        src = cp.asarray(depth_mm.ravel(), dtype=cp.uint16)
        blk = (16, 16)
        grd = ((self.W + 15) // 16, (self.H + 15) // 16)
        _k_bilateral(grd, blk, (src, self._dbuf, self.W, self.H,
                                np.float32(4.5), np.float32(30.0), np.int32(3)))
        return self._dbuf

    # -- pipeline ------------------------------------------------------------

    def integrate(self, depth_gpu: cp.ndarray, c2w: np.ndarray):
        w2c = cp.asarray(np.linalg.inv(c2w).astype(np.float32).ravel())
        total = self.NX * self.NY * self.NZ
        threads = 256
        _k_integrate(((total + threads - 1) // threads,), (threads,),
                     (self.tsdf, self.weight, depth_gpu,
                      np.int32(self.NX), np.int32(self.NY), np.int32(self.NZ),
                      np.float32(self.origin[0]), np.float32(self.origin[1]),
                      np.float32(self.origin[2]), np.float32(self.voxel),
                      w2c,
                      np.float32(self.fx), np.float32(self.fy),
                      np.float32(self.cx), np.float32(self.cy),
                      np.int32(self.W), np.int32(self.H),
                      np.float32(self.trunc), np.float32(self.dmin),
                      np.float32(self.dmax), np.float32(self.wmax)))

    def integrate_rgb(self, depth_gpu: cp.ndarray, bgra: np.ndarray, c2w: np.ndarray):
        self._ensure_colour()
        self._cbuf.set(np.ascontiguousarray(bgra).ravel())
        w2c = cp.asarray(np.linalg.inv(c2w).astype(np.float32).ravel())
        total = self.NX * self.NY * self.NZ
        threads = 256
        _k_integrate_rgb(((total + threads - 1) // threads,), (threads,),
                         (self.tsdf, self.weight, self.colour, depth_gpu, self._cbuf,
                          np.int32(self.NX), np.int32(self.NY), np.int32(self.NZ),
                          np.float32(self.origin[0]), np.float32(self.origin[1]),
                          np.float32(self.origin[2]), np.float32(self.voxel),
                          w2c,
                          np.float32(self.fx), np.float32(self.fy),
                          np.float32(self.cx), np.float32(self.cy),
                          np.int32(self.W), np.int32(self.H),
                          np.float32(self.trunc), np.float32(self.dmin),
                          np.float32(self.dmax), np.float32(self.wmax),
                          np.float32(CU_A), np.float32(CU_B), np.float32(CU_C),
                          np.float32(CV_A), np.float32(CV_C)))

    def sample_colour(self, pts_world: np.ndarray) -> np.ndarray:
        """Vertex colours for mesh points, as float32 RGB in 0..1.

        Sampled on the GPU: the colour volume is hundreds of megabytes, so we
        index it there and bring back only the few hundred thousand results.
        """
        if self.colour is None or not len(pts_world):
            return np.full((len(pts_world), 3), 0.72, dtype=np.float32)
        ox, oy, oz = self.origin
        p = cp.asarray(pts_world, dtype=cp.float32)
        ix = cp.clip(((p[:, 0] - ox) / self.voxel).astype(cp.int32), 0, self.NX - 1)
        iy = cp.clip(((p[:, 1] - oy) / self.voxel).astype(cp.int32), 0, self.NY - 1)
        iz = cp.clip(((p[:, 2] - oz) / self.voxel).astype(cp.int32), 0, self.NZ - 1)
        idx = (iz * self.NY + iy) * self.NX + ix
        rgb = cp.stack([self.colour[idx * 3 + k] for k in range(3)], axis=1) / 255.0
        return cp.asnumpy(cp.clip(rgb, 0.0, 1.0)).astype(np.float32)

    def ray_range(self, c2w: np.ndarray):
        """Near/far for a ray march from this camera that actually reaches the
        volume.

        The sensor's dmin/dmax are distances from the *sensor*. Raycasting a
        preview from the viewer's camera with those limits means rays stop short
        of the volume entirely, which produces a scatter of stray hits rather
        than the model. Derive the range from the camera to the volume's corners
        instead.
        """
        eye = np.asarray(c2w, dtype=np.float32).reshape(4, 4)[:3, 3]
        ox, oy, oz = self.origin
        s3 = self.NX * self.voxel
        corners = np.array([[ox + i * s3, oy + j * s3, oz + k * s3]
                            for i in (0, 1) for j in (0, 1) for k in (0, 1)],
                           dtype=np.float32)
        d = np.linalg.norm(corners - eye, axis=1)
        # Near must stay at the camera. Starting it partway to the volume (an
        # earlier attempt at saving steps) silently clips whatever is closer
        # than that, so the visible model changed shape as the viewer orbited.
        # Marching the extra distance costs about a millisecond.
        return 0.05, float(d.max()) * 1.05

    def raycast(self, c2w: np.ndarray, near=None, far=None):
        c2w_g = cp.asarray(c2w.astype(np.float32).ravel())
        nr = self.dmin if near is None else near
        fr = self.dmax if far is None else far
        blk = (16, 16)
        grd = ((self.W + 15) // 16, (self.H + 15) // 16)
        _k_raycast(grd, blk,
                   (self.tsdf, self.weight,
                    np.int32(self.NX), np.int32(self.NY), np.int32(self.NZ),
                    np.float32(self.origin[0]), np.float32(self.origin[1]),
                    np.float32(self.origin[2]), np.float32(self.voxel),
                    c2w_g,
                    np.float32(self.fx), np.float32(self.fy),
                    np.float32(self.cx), np.float32(self.cy),
                    np.int32(self.W), np.int32(self.H),
                    np.float32(self.trunc), np.float32(nr), np.float32(fr),
                    self._vmap, self._nmap))
        return self._vmap, self._nmap

    def render(self, c2w: np.ndarray, W: int, H: int, fov_y: float):
        """Shade the volume from an arbitrary camera. Returns (H, W, 3) uint8.

        This is how a KinectFusion-derived scanner draws its live view, and why
        those previews look like a solid object rather than a cloud of dots. A
        ray per pixel walks the volume to the zero crossing and shades the
        surface normal there, so the result is continuous by construction: there
        are no samples to leave gaps between.

        Splatting points was the alternative and it cannot get there. Points are
        drawn at the voxel centres, so the picture is only as dense as the grid,
        and the preview is decimated on top of that. Bigger splats hide the gaps
        at the cost of looking like bigger squares.

        The camera is the viewer's, not the sensor's, which is what an earlier
        raycast preview got wrong: it used the sensor intrinsics whatever the
        viewer was doing, so the image did not match the scene being orbited.
        Intrinsics are derived from the requested size and field of view, and
        buffers are cached per size because a resize should not reallocate on
        every frame.
        """
        W, H = int(W), int(H)
        key = (W, H)
        if getattr(self, '_rc_key', None) != key:
            self._rc_key = key
            self._rc_v = cp.empty((H * W * 3,), dtype=cp.float32)
            self._rc_n = cp.empty((H * W * 3,), dtype=cp.float32)
        fy = (H * 0.5) / float(np.tan(fov_y * 0.5))
        fx = fy
        blk = (16, 16)
        grd = ((W + 15) // 16, (H + 15) // 16)
        _k_raycast(grd, blk,
                   (self.tsdf, self.weight,
                    np.int32(self.NX), np.int32(self.NY), np.int32(self.NZ),
                    np.float32(self.origin[0]), np.float32(self.origin[1]),
                    np.float32(self.origin[2]), np.float32(self.voxel),
                    cp.asarray(c2w.astype(np.float32).ravel()),
                    np.float32(fx), np.float32(fy),
                    np.float32(W * 0.5), np.float32(H * 0.5),
                    np.int32(W), np.int32(H),
                    np.float32(self.trunc), np.float32(0.05),
                    np.float32(max(self.dmax * 3.0, 8.0)),
                    self._rc_v, self._rc_n))

        n = self._rc_n.reshape(H, W, 3)
        hit = cp.isfinite(n[:, :, 0])
        # Two lights: a fixed key so shape reads while orbiting, and a dim fill
        # from the camera so surfaces facing away do not go pure black.
        L = cp.asarray([-0.35, 0.78, 0.52], dtype=cp.float32)
        d = cp.abs(n[:, :, 0] * L[0] + n[:, :, 1] * L[1] + n[:, :, 2] * L[2])
        d = cp.nan_to_num(d)
        v = 0.16 + 0.84 * d * d
        tint = cp.asarray([0.86, 0.98, 0.93], dtype=cp.float32)
        rgb = (v[:, :, None] * tint[None, None, :] * 255.0)
        rgb = cp.where(hit[:, :, None], rgb, 0.0)
        out = cp.zeros((H, W, 4), dtype=cp.uint8)
        out[:, :, :3] = cp.clip(rgb, 0, 255).astype(cp.uint8)
        out[:, :, 3] = 255
        # Rows go out bottom-up. A texture is sampled from the bottom left and
        # the raycast walks from the top, so flipping here means the client can
        # upload the bytes as they arrive.
        return cp.asnumpy(out[::-1])

    def track(self, depth_gpu: cp.ndarray, c2w_init: np.ndarray,
              c2w_model: np.ndarray, iterations=10, dist_thresh=0.05):
        """Point-to-plane ICP of the frame against a raycast of the model.

        Returns (pose, fitness, rmse, conditioning).

        Conditioning is the smallest eigenvalue of the normal equations over the
        largest, and it answers a question fitness cannot: whether the shape in
        view is capable of pinning a pose down at all. A flat wall, or a smooth
        cylinder seen from one side, matches its own raycast beautifully at any
        position along the degenerate direction, so ICP reports a high fitness
        and a low error while the pose slides freely. That is the failure that
        smears a turntable subject into a solid of revolution, and it is
        invisible to every other number the solver produces.

        The six eigenvalues are not in comparable units (the rotation block
        carries an extra factor of metres from the cross products), so the ratio
        is not a true condition number and only means anything against an
        empirical threshold. It is still monotonic in the thing we care about,
        which is what the check needs.
        """
        vmap, nmap = self.raycast(c2w_model)
        pose = c2w_init.astype(np.float32).copy()
        w2c_model = cp.asarray(np.linalg.inv(c2w_model).astype(np.float32).ravel())

        blk = (16, 16)
        grd = ((self.W + 15) // 16, (self.H + 15) // 16)
        npix = self.W * self.H

        # Fitness must be measured against pixels that could possibly match:
        # ones carrying depth inside the working range. Dividing by the whole
        # frame instead means a subject occupying 15% of the view can never
        # score above 0.15, and every good registration gets binned as lost.
        fitness = 0.0
        rmse = 0.0
        cond = 0.0

        for _ in range(iterations):
            c2w_g = cp.asarray(pose.ravel())
            _k_icp(grd, blk,
                   (depth_gpu, vmap, nmap, c2w_g, w2c_model,
                    np.float32(self.fx), np.float32(self.fy),
                    np.float32(self.cx), np.float32(self.cy),
                    np.int32(self.W), np.int32(self.H),
                    np.float32(self.dmin), np.float32(self.dmax),
                    np.float32(dist_thresh), np.float32(0.5),
                    np.float32(self.origin[0]), np.float32(self.origin[1]),
                    np.float32(self.origin[2]), np.float32(self.NX * self.voxel),
                    self._A, self._b, self._valid, self._inbox))

            m = self._valid.astype(cp.float32)
            n_used = int(m.sum().item())
            # Denominator is pixels that landed inside the volume, so fitness
            # means "how much of the subject matched", not "how much of the room".
            denom = max(int(self._inbox.sum().item()), 1)
            # Kept for the caller: "nothing is in the box" and "things are in the
            # box but nothing matches" are different failures with different
            # fixes, and fitness alone cannot tell them apart.
            self.inbox = denom
            fitness = n_used / float(denom)
            if n_used < 1000:
                break

            A = self._A.reshape(npix, 6) * m[:, None]
            b = self._b * m
            AtA = cp.asnumpy(A.T @ A).astype(np.float64)
            Atb = cp.asnumpy(A.T @ b).astype(np.float64)
            rmse = float(cp.sqrt((b * b).sum() / max(n_used, 1)).item())

            # Normalised by the number of correspondences so the figure means
            # the same thing whether the subject fills the frame or a corner of it.
            ev = np.linalg.eigvalsh(AtA / max(n_used, 1))
            top = float(ev[-1])
            cond = float(ev[0] / top) if top > 1e-12 else 0.0

            try:
                xi = np.linalg.solve(AtA + np.eye(6) * 1e-6, Atb)
            except np.linalg.LinAlgError:
                break
            if not np.all(np.isfinite(xi)):
                break

            pose = (_se3(xi) @ pose).astype(np.float32)
            if np.linalg.norm(xi[:3]) < 1e-5 and np.linalg.norm(xi[3:]) < 1e-5:
                break

        return pose, fitness, rmse, cond

    # -- extraction ----------------------------------------------------------

    def surface_voxels(self) -> int:
        """Voxels sitting on the zero crossing. A reduction over the whole grid
        on the GPU, well under a millisecond, so it is fine per frame."""
        n = ((self.weight > 0) & (cp.abs(self.tsdf) < 0.35)).sum()
        self.surface_count = int(n.item())
        return self.surface_count

    def points(self, max_points=260_000):
        """Every surface voxel: (world-space xyz, surface normals).

        This is the model itself rather than a render of it. An earlier version
        shipped a server-side raycast instead, which used the sensor's field of
        view no matter where the viewer was standing, so the preview changed
        size and shape as you orbited. Sending the points and letting the client
        project them removes that entirely.

        Kept on the GPU until the last step: the mask is over tens of millions
        of voxels, and only the survivors are worth moving.
        """
        surf = (self.weight > 0) & (cp.abs(self.tsdf) < 0.35)
        flat = cp.flatnonzero(surf)
        n = int(flat.size)
        self.surface_count = n
        if n == 0:
            return (np.zeros((0, 3), dtype=np.float32),
                    np.zeros((0, 3), dtype=np.float32))
        if n > max_points:
            flat = flat[cp.linspace(0, n - 1, max_points).astype(cp.int64)]

        nxny = self.NX * self.NY
        z = (flat // nxny).astype(cp.float32)
        rem = flat - (flat // nxny) * nxny
        y = (rem // self.NX).astype(cp.float32)
        x = (rem - (rem // self.NX) * self.NX).astype(cp.float32)

        ox, oy, oz = self.origin
        pts = cp.stack([ox + (x + 0.5) * self.voxel,
                        oy + (y + 0.5) * self.voxel,
                        oz + (z + 0.5) * self.voxel], axis=1)

        m = int(flat.size)
        nrm = cp.zeros((m * 3,), dtype=cp.float32)
        threads = 256
        _k_normals(((m + threads - 1) // threads,), (threads,),
                   (self.tsdf, self.weight, flat.astype(cp.int64), np.int32(m),
                    np.int32(self.NX), np.int32(self.NY), np.int32(self.NZ), nrm))
        return (cp.asnumpy(pts).astype(np.float32),
                cp.asnumpy(nrm).reshape(m, 3).astype(np.float32))

    def mesh(self):
        """Marching cubes over the volume. Returns (verts, faces, normals).

        Marching cubes is the one step that runs on the host, so the volume has
        to come back off the card. It used to arrive as three separate float32
        arrays, the distances, the weights, and then the masked combination of
        the two, which at 1267 cubed is 7.58 GiB each and 22.7 GiB in total for
        a step that needs one of them. Machines ran out of RAM long before the
        card ran out of VRAM.

        The mask is applied on the GPU instead, a slab at a time, straight into
        the single host array that marching cubes reads.
        """
        from skimage import measure

        n = self.NX * self.NY * self.NZ
        need = _mesh_host_bytes(n)
        free = host_free()
        if free is not None and need > free:
            raise MemoryError(
                'Meshing %d³ voxels needs about %.1f GB of system memory '
                'and only %.1f GB is free. Marching cubes runs on the CPU, so '
                'this is RAM rather than VRAM. Re-fuse at %.1f mm or larger, '
                'or close something.'
                % (self.NX, need / 1e9, free / 1e9,
                   _coarsest_mm(self.NX * self.voxel, free)))

        vol = np.empty(n, dtype=np.float32)
        # 64M elements is a 256 MB staging buffer on the card, which is small
        # beside the volume already sitting there.
        step = 64 * 1024 * 1024
        for i in range(0, n, step):
            j = min(i + step, n)
            chunk = cp.where(self.weight[i:j] > 0, self.tsdf[i:j],
                             cp.float32(1.0))
            vol[i:j] = cp.asnumpy(chunk)
            del chunk
        vol = vol.reshape(self.NZ, self.NY, self.NX)

        verts, faces, normals, _ = measure.marching_cubes(
            vol, level=0.0, spacing=(self.voxel, self.voxel, self.voxel),
            allow_degenerate=False)
        # marching_cubes indexes (z, y, x); put it back to (x, y, z) world space
        ox, oy, oz = self.origin
        v = np.empty_like(verts)
        v[:, 0] = verts[:, 2] + ox
        v[:, 1] = verts[:, 1] + oy
        v[:, 2] = verts[:, 0] + oz
        nrm = np.empty_like(normals)
        nrm[:, 0] = normals[:, 2]
        nrm[:, 1] = normals[:, 1]
        nrm[:, 2] = normals[:, 0]
        return (v.astype(np.float32), faces.astype(np.uint32), nrm.astype(np.float32))
