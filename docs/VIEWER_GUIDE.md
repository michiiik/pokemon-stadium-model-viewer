# Viewer guide

The sidebar lists models from the configured provider. Search by model file,
Pokédex ID, canonical name, or provider alias (s1 and s2). In dual mode each
provider has its own tab and selection state.

The toolbar supports play/pause, looping, frame stepping, speed, and timeline
scrubbing. Drag to orbit, Shift-drag or right-drag to pan, and use the wheel
to zoom. Front, side, top, reset, textures, lighting, wireframe, axes, bounds,
skeleton, and bone-name controls are available in the viewport.

The server exposes only normalized JSON model data and static viewer files.
Asset references are constrained to their provider root; browser requests
cannot traverse the filesystem. Diagnostics for unsupported commands or
truncated resources are shown in the resource report.

The viewer consumes extracted Stadium 1 BinArchive/PERS-SZP/FRAGMENT resources
and Stadium 2 extraction caches written by tools/stadium2_extract.py. It is an
inspection viewer, not a replacement for an emulator or a complete N64 RDP
implementation.
