# Brand assets

Drop the Ghostlight logo files in here. The app looks for these exact names:

| File | Used for | Notes |
|---|---|---|
| `mark.svg` | The ghost mark alone, top-left of the app | SVG preferred. `mark.png` also works. |
| `logo.svg` | Full lockup (ghost + wordmark) | Used on the About screen. `logo.png` also works. |
| `favicon.svg` | Browser tab icon | Needs an explicit `fill`, not `currentColor`. A favicon has no CSS context, so `currentColor` resolves to black and disappears on a dark tab strip. |

If a file is missing the app falls back to a built-in inline ghost, so nothing breaks.

Transparent background please. The app renders on a near-black surface (`#090A0B`).
