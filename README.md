# Shred Looper

Self-contained offline guitar practice looper (PWA).

Edit `src/` -> `python3 src/build.py` -> commit. `index.html` is BUILT output;
never hand-edit it. MIDI is parsed as binary by mido in the build; the runtime
ships zero .mid files (all note data is baked JSON).

Deploy: push to main; GitHub Pages serves the repo root.
