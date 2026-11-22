# Client collision data

This directory is populated on demand; the extracted files are large and are
not committed to the repository.

```powershell
# Northshire / Elwynn starter area (map 0, tile 48,32 +/- 2 tiles)
python tools/fetch_client_data.py --preset starter

# A larger area or another map
python tools/fetch_client_data.py --map 0 --around -8955,-132 --radius 4
python tools/fetch_client_data.py --map 1 --tile 42,25
```

The server reads the directory from `worldserver.client_data` in
`config/config.yaml` (default `./data/client`).  With an empty directory the
server still runs, but line of sight falls back to the optional
`sight_blockers` table and creatures move in straight lines.

Layout expected by the runtime readers:

| Path | Contents |
| --- | --- |
| `maps/003xxyy.map` | Terrain heightmap and holes |
| `vmaps/003.vmtree` | Global VMap tree (not required by the runtime reader) |
| `vmaps/003_yy_xx.vmtile` | WMO/M2 placements (note swapped tile order) |
| `vmaps/<model>.vmo` | Per-model collision groups |
| `mmaps/003xxyy.mmtile` | Recast/Detour navigation mesh |

The default mirror is `Torrer/TrinityCore-3.3.5-data`, a public 3.3.5a
(12340) extract.  Use `--source` to point at a different mirror with the same
layout.
