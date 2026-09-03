# Format boundary

The viewer follows the extracted-resource path:

~~~text
BinArchive -> PERS-SZP/Yay0 -> FRAGMENT -> model root -> GeoLayout -> F3DEX2
~~~

Stadium 1 discovers extracted model resources and supports the common texture,
geometry, graph, animation, and expression-track structures used by the
Pokémon model set. Stadium 2 has a separate indexed model/pose bank and is
served through its own provider. Stadium 2 poses are decoded into the shared
transform-curve representation.

The Stadium 2 helper writes a manifest plus decoded model records and
byte-preserved pose records. This cache is a local build artifact and must
remain outside Git. The viewer reads only the selected record at request time.

Unknown pointers, commands, formats, truncated resources, and unsupported
animation slots become diagnostics rather than crashing the server. The
renderer deliberately does not claim full game-renderer equivalence, exact
runtime material state, or complete ROM-loading support.

Stadium 1 can read the source-defined model archive directly from a user-owned ROM or use an extracted pokemon_models directory. Stadium 2
can be prepared directly from a user-owned ROM with tools/setup_viewer.py.
