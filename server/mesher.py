"""
Mesh cleanup and export.

Surface extraction itself now happens on the GPU: marching cubes over the TSDF
in tracker.mesh(). That is both faster and better than the Poisson-over-points
approach this file used to do, because the TSDF has already averaged out the
sensor noise that Poisson was being asked to smooth over.

What is left here is the Refine stage's operation stack and file output, which
is Open3D doing what it is good at. Operations always rebuild from the raw
marching cubes result, so the stack is non-destructive: toggle something off
and it comes back.

Everything in this module is already in the GL convention (Y up, -Z forward);
tracker.to_gl() is applied once when the mesh leaves the volume.
"""

from __future__ import annotations

import numpy as np
import open3d as o3d


def from_arrays(verts: np.ndarray, faces: np.ndarray,
                normals: np.ndarray | None = None,
                colours: np.ndarray | None = None) -> o3d.geometry.TriangleMesh:
    m = o3d.geometry.TriangleMesh(
        o3d.utility.Vector3dVector(verts.astype(np.float64)),
        o3d.utility.Vector3iVector(faces.astype(np.int32)))
    if normals is not None and len(normals) == len(verts):
        m.vertex_normals = o3d.utility.Vector3dVector(normals.astype(np.float64))
    else:
        m.compute_vertex_normals()
    if colours is not None and len(colours) == len(verts):
        m.vertex_colors = o3d.utility.Vector3dVector(colours.astype(np.float64))
    return m


def cut_plane(mesh: o3d.geometry.TriangleMesh, normal, offset: float,
              keep_above=True, margin=0.002) -> o3d.geometry.TriangleMesh:
    """Remove everything on one side of a plane.

    Meant for the turntable. A platter is flat, static and right up against the
    subject, so a bounding box can only cut it away by also cutting the bottom
    of the subject. A plane follows the actual surface, including when the
    sensor is looking at it on a slant, which a box aligned to the world axes
    never can.

    A vertex is judged, and a triangle goes only if all three of its vertices
    do. Dropping a triangle when any vertex is below the plane would nibble a
    ragged edge into the subject wherever it meets the platter.
    """
    n = np.asarray(normal, dtype=np.float64).reshape(3)
    ln = np.linalg.norm(n)
    if ln < 1e-9 or not len(mesh.vertices):
        return mesh
    n = n / ln
    m = o3d.geometry.TriangleMesh(mesh)
    h = np.asarray(m.vertices) @ n + offset
    doomed = (h < margin) if keep_above else (h > -margin)
    tris = np.asarray(m.triangles)
    if not len(tris):
        return m
    m.remove_triangles_by_mask(doomed[tris].all(axis=1))
    m.remove_unreferenced_vertices()
    return m


def apply_ops(mesh: o3d.geometry.TriangleMesh, ops: dict,
              bbox: float | None = None,
              plane: tuple | None = None) -> o3d.geometry.TriangleMesh:
    """Apply the Refine stage's operation stack. Returns a new mesh."""
    m = o3d.geometry.TriangleMesh(mesh)

    if ops.get('crop') and bbox:
        c = m.get_center()
        h = bbox / 2.0
        m = m.crop(o3d.geometry.AxisAlignedBoundingBox(c - h, c + h))

    # Before islands, deliberately. Cutting the platter away is usually what
    # separates the subject from the surface it is sitting on, and until that
    # happens they are one connected component and "keep the largest" keeps both.
    if ops.get('turntable') and plane:
        m = cut_plane(m, plane[0], plane[1])

    if ops.get('islands'):
        labels, counts, _ = m.cluster_connected_triangles()
        labels = np.asarray(labels)
        counts = np.asarray(counts)
        if counts.size:
            m.remove_triangles_by_mask(labels != int(counts.argmax()))
            m.remove_unreferenced_vertices()

    if ops.get('smooth'):
        # Taubin rather than Laplacian: it smooths without the shrinkage that
        # would quietly change the object's dimensions.
        m = m.filter_smooth_taubin(number_of_iterations=6)

    if ops.get('simplify'):
        target = max(2000, len(m.triangles) // 2)
        m = m.simplify_quadric_decimation(target_number_of_triangles=target)

    m.remove_degenerate_triangles()
    m.remove_duplicated_triangles()
    m.remove_duplicated_vertices()
    m.remove_non_manifold_edges()
    m.compute_vertex_normals()
    return m


def stats(mesh: o3d.geometry.TriangleMesh) -> dict:
    extent = mesh.get_axis_aligned_bounding_box().get_extent() if len(mesh.vertices) else [0, 0, 0]
    return {
        'vertices': len(mesh.vertices),
        'faces': len(mesh.triangles),
        'watertight': bool(mesh.is_watertight()),
        'coloured': bool(mesh.has_vertex_colors()),
        'size_mm': [round(float(e) * 1000, 1) for e in extent],
    }


def wire(mesh: o3d.geometry.TriangleMesh, max_faces=400_000):
    """Mesh as (positions, normals, colours f32, indices u32) for the front end."""
    m = mesh
    if len(m.triangles) > max_faces:
        m = o3d.geometry.TriangleMesh(mesh).simplify_quadric_decimation(max_faces)
        m.compute_vertex_normals()
    v = np.asarray(m.vertices, dtype=np.float32)
    n = np.asarray(m.vertex_normals, dtype=np.float32)
    if m.has_vertex_colors():
        c = np.asarray(m.vertex_colors, dtype=np.float32)
    else:
        c = np.full_like(v, 0.72, dtype=np.float32)
    return v, n, c, np.asarray(m.triangles, dtype=np.uint32)


def save(mesh: o3d.geometry.TriangleMesh, path: str) -> bool:
    """Write STL/PLY/OBJ/GLB. STL needs face normals or Open3D refuses."""
    if path.lower().endswith('.stl') and not mesh.has_triangle_normals():
        mesh.compute_triangle_normals()
    return o3d.io.write_triangle_mesh(path, mesh, write_ascii=False, compressed=True)
