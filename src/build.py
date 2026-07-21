#!/usr/bin/env python3
"""Shred Looper build: MIDI -> JSON -> inject into template -> index.html.

Single source of truth for the baked app. Every edit = change src/, run this,
commit. MIDI is opened strictly as binary via mido; runtime needs zero .mid.
"""
import base64
import hashlib
import io
import json
import math
import re
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import mido

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent

BEATS_PER_BAR = 4
PW_RANGE_SEMIS = 2.0  # both MIDIs carry full-scale pitchwheel at +/-2 semis

# ---------------------------------------------------------------- MIDI parse

def parse_song(path, track_idx, trim_before_beat=None):
    """Extract notes {b,d,p,v,bd} from one track. Beats are quarter notes."""
    m = mido.MidiFile(str(path))  # binary parse; never treat as text
    tpb = m.ticks_per_beat
    track = m.tracks[track_idx]

    abs_t = 0
    events = []  # (beat, msg)
    for msg in track:
        abs_t += msg.time
        events.append((abs_t / tpb, msg))

    pw = [(b, msg.pitch / 8192.0 * PW_RANGE_SEMIS)
          for b, msg in events if msg.type == 'pitchwheel']

    notes = []
    open_notes = {}  # pitch -> (onset_beat, velocity)
    for b, msg in events:
        if msg.type == 'note_on' and msg.velocity > 0:
            open_notes[msg.note] = (b, msg.velocity)
        elif msg.type in ('note_off', 'note_on'):
            if msg.note in open_notes:
                on_b, vel = open_notes.pop(msg.note)
                notes.append({'b': on_b, 'd': max(0.01, b - on_b),
                              'p': msg.note, 'v': vel})

    # attach bend curves: pw samples within [onset, onset+dur]
    for n in notes:
        a, z = n['b'], n['b'] + n['d']
        curve = []
        prev_val = 0.0
        for b, semis in pw:
            if b < a:
                prev_val = semis
                continue
            if b > z:
                break
            curve.append([b - a, semis])
        if prev_val and (not curve or curve[0][0] > 1e-6):
            curve.insert(0, [0.0, prev_val])
        # simplify: keep endpoints + direction-preserving deltas >= 0.03 semis
        if curve:
            slim = [curve[0]]
            for pt in curve[1:-1]:
                if abs(pt[1] - slim[-1][1]) >= 0.03:
                    slim.append(pt)
            if len(curve) > 1:
                slim.append(curve[-1])
            slim = [[round(o, 3), round(s, 2)] for o, s in slim]
            slim = [pt for i, pt in enumerate(slim)
                    if i == 0 or pt != slim[i - 1]]
            if len(slim) > 1 or (slim and abs(slim[0][1]) >= 0.03):
                n['bd'] = slim[:32]

    if trim_before_beat is not None:
        notes = [n for n in notes if n['b'] >= trim_before_beat]
        for n in notes:
            n['b'] -= trim_before_beat

    for n in notes:
        n['b'] = round(n['b'], 4)
        n['d'] = round(n['d'], 4)
    notes.sort(key=lambda n: (n['b'], n['p']))
    return notes

# ------------------------------------------------------------ difficulty

MIN_SPAN = 1.0 * BEATS_PER_BAR  # approved: full 1-bar minimum window

def _score(onsets, i, j, tempo):
    n = j - i + 1
    span = onsets[j] - onsets[i]
    if n < 2 or span <= 0:
        return 0.0
    return tempo * 0.25 / (span / (n - 1))

def hardest_window(notes, tempo, exclude_before_beat=0.0):
    onsets = sorted(set(round(n['b'], 6) for n in notes))
    best = None
    for i in range(len(onsets)):
        if onsets[i] < exclude_before_beat:
            continue
        j = None
        for k in range(i + 1, len(onsets)):
            if onsets[k] - onsets[i] >= MIN_SPAN:
                j = k
                break
        if j is None:
            continue
        sc = _score(onsets, i, j, tempo)
        while j + 1 < len(onsets):
            nxt = _score(onsets, i, j + 1, tempo)
            if nxt > sc:
                sc, j = nxt, j + 1
            else:
                break
        if best is None or sc > best[0]:
            best = (sc, onsets[i], onsets[j])
    sc, a, b = best
    return {'a': a, 'b': b, 'eq16': round(sc)}

def diff_emoji(eq16):
    if eq16 < 120: return '🟢'      # easy
    if eq16 < 160: return '🟡'      # medium
    if eq16 < 180: return '🟠'      # hard
    return '🔴'                      # really hard

# ------------------------------------------------------------------ drums

def drum_data_uri(path, keep_sec, sr=22050):
    """afconvert to mono 16-bit @sr, trim leading silence + cap length,
    10ms fade-out, return wav data URI."""
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tf:
        tmp = tf.name
    subprocess.run(['afconvert', '-f', 'WAVE', '-d', f'LEI16@{sr}', '-c', '1',
                    str(path), tmp], check=True, capture_output=True)
    with wave.open(tmp, 'rb') as w:
        raw = w.readframes(w.getnframes())
    smp = list(struct.unpack('<%dh' % (len(raw) // 2), raw))
    # trim leading silence (below ~1.5% full scale)
    thresh = int(32767 * 0.015)
    start = next((i for i, s in enumerate(smp) if abs(s) > thresh), 0)
    start = max(0, start - int(0.002 * sr))
    smp = smp[start:start + int(keep_sec * sr)]
    fade = int(0.010 * sr)
    for i in range(fade):
        smp[-fade + i] = int(smp[-fade + i] * (1 - (i + 1) / fade))
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(struct.pack('<%dh' % len(smp), *smp))
    b64 = base64.b64encode(buf.getvalue()).decode()
    return 'data:audio/wav;base64,' + b64

# ------------------------------------------------------------------ icons

def make_icon_png(size, bg, fg):
    """Tiny solid icon with a lightning-ish bolt, no PIL text/emoji needed."""
    from PIL import Image, ImageDraw
    im = Image.new('RGB', (size, size), bg)
    d = ImageDraw.Draw(im)
    s = size
    bolt = [(0.58*s, 0.08*s), (0.30*s, 0.55*s), (0.47*s, 0.55*s),
            (0.40*s, 0.92*s), (0.72*s, 0.42*s), (0.53*s, 0.42*s)]
    d.polygon(bolt, fill=fg)
    buf = io.BytesIO()
    im.save(buf, 'PNG', optimize=True)
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()

# ------------------------------------------------------------------- main

def pickup_start(notes, before_beat, count):
    """Beat of the Nth distinct onset before `before_beat` (pickup anchor)."""
    onsets = sorted(set(n['b'] for n in notes if n['b'] < before_beat))
    return onsets[-count]

def half_bar_len(a, b):
    """Exact start; length rounded UP to nearest half-bar (2 beats)."""
    return a + math.ceil((b - a) / 2.0 - 1e-9) * 2

def midi_bpm(path):
    m = mido.MidiFile(str(path))
    for t in m.tracks:
        for msg in t:
            if msg.type == 'set_tempo':
                return round(mido.tempo2bpm(msg.tempo), 1)
    return 120.0


def main():
    ct_notes = parse_song(SRC / 'midi' / 'CRAZY_TRAIN_MANUALLY.mid', 3)
    ds_notes = parse_song(SRC / 'midi' / 'dolphinshoalsmanual.mid', 2,
                          trim_before_beat=(18 - 1) * 4)
    tw_notes = parse_song(SRC / 'midi' / 'the_world.mid', 2)

    # ---- Crazy Train ----
    ct_hard = hardest_window(ct_notes, 138.7,
                             exclude_before_beat=2 * BEATS_PER_BAR)  # skip bars 1-2 tapping run
    ct = {
        'id': 'crazy', 'name': 'Crazy Train', 'emoji': '🚂', 'theme': 'rhoads',
        'origBpm': 138.7, 'barOffset': 0,
        'totalBars': 16,
        'notes': ct_notes,
        'sections': [
            {'name': 'Outro run', 'a': 56.0, 'b': half_bar_len(56.0, 64.0)},
        ],
    }

    # ---- Dolphin Shoals (timeline beat 0 == label bar 18) ----
    def L(label_bar):  # label bar -> timeline beat
        return (label_bar - 18) * 4.0
    pr1 = pickup_start(ds_notes, L(35), 2)
    pr2 = pickup_start(ds_notes, L(37), 2)
    lick = pickup_start(ds_notes, L(39), 3)
    ds_hard = hardest_window(ds_notes, 138.0)
    ds_last = max(n['b'] + n['d'] for n in ds_notes)
    ds = {
        'id': 'dolphin', 'name': 'Dolphin Shoals', 'emoji': '🌴', 'theme': 'dolphin',
        'origBpm': 138.0, 'barOffset': 17,
        'totalBars': int(math.ceil(ds_last / 4.0)),
        'notes': ds_notes,
        'sections': [
            {'name': 'Tricky Middle', 'a': L(27), 'b': half_bar_len(L(27), L(31))},
            {'name': 'Pre Run 1', 'a': pr1, 'b': half_bar_len(pr1, L(37))},
            {'name': 'Pre Run 2', 'a': pr2, 'b': half_bar_len(pr2, L(39))},
            {'name': 'The Lick', 'a': lick, 'b': half_bar_len(lick, L(41))},
            {'name': 'All 3 Runs', 'a': pr1, 'b': half_bar_len(pr1, L(41))},
        ],
    }

    # ---- The World (Austin's own arrangement; labels from bar 1) ----
    tw_bpm = midi_bpm(SRC / 'midi' / 'the_world.mid')
    tw_hard = hardest_window(tw_notes, tw_bpm)
    tw_last = max(n['b'] + n['d'] for n in tw_notes)
    tw = {
        'id': 'world', 'name': 'The World', 'emoji': '🎱', 'theme': 'world',
        'origBpm': tw_bpm, 'barOffset': 0,
        'totalBars': int(math.ceil(tw_last / 4.0)),
        'notes': tw_notes,
        'sections': [],
    }

    for song, hard in ((ct, ct_hard), (ds, ds_hard), (tw, tw_hard)):
        a, b = hard['a'], hard['b']
        song['hardest'] = {
            'a': a, 'b': half_bar_len(a, b), 'eq16': hard['eq16'],
            'emoji': diff_emoji(hard['eq16']),
        }
        for s in song['sections'] + [song['hardest']]:
            s['a'] = round(s['a'], 4); s['b'] = round(s['b'], 4)

    drums = {
        'kick': drum_data_uri(SRC / 'drums' / 'kick.wav', 0.35),
        'snare': drum_data_uri(SRC / 'drums' / 'snare.wav', 0.35),
        'hat': drum_data_uri(SRC / 'drums' / 'hat.wav', 0.22),
    }

    src_hash = hashlib.sha1()
    for p in sorted(SRC.rglob('*')):
        if p.is_file():
            src_hash.update(p.read_bytes())
    cache_version = 'sl-' + src_hash.hexdigest()[:10]

    icon192 = make_icon_png(192, '#111111', '#e6b93c')
    icon512 = make_icon_png(512, '#111111', '#e6b93c')

    data = {'cacheVersion': cache_version, 'songs': [ct, ds, tw], 'drums': drums}

    template = (SRC / 'template.html').read_text()
    html = (template
            .replace('"__DATA_JSON__"', json.dumps(data, separators=(',', ':')))
            .replace('__CACHE_VERSION__', cache_version)
            .replace('__ICON192__', icon192))
    (ROOT / 'index.html').write_text(html)

    manifest = {
        'name': 'Shred Looper', 'short_name': 'Shred',
        'start_url': './index.html', 'display': 'standalone',
        'orientation': 'portrait', 'background_color': '#111111',
        'theme_color': '#111111',
        'icons': [
            {'src': icon192, 'sizes': '192x192', 'type': 'image/png',
             'purpose': 'any maskable'},
            {'src': icon512, 'sizes': '512x512', 'type': 'image/png',
             'purpose': 'any maskable'},
        ],
    }
    (ROOT / 'manifest.webmanifest').write_text(json.dumps(manifest, indent=1))

    sw = (SRC / 'sw.template.js').read_text().replace('__CACHE_VERSION__',
                                                      cache_version)
    (ROOT / 'sw.js').write_text(sw)

    # ---- verification: every <script> block must compile in node ----
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
    ok = True
    for i, s in enumerate(scripts):
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as tf:
            tf.write('new Function(' + json.dumps(s) + ');')
            tf_path = tf.name
        r = subprocess.run(['node', tf_path], capture_output=True, text=True)
        if r.returncode != 0:
            ok = False
            print(f'SCRIPT BLOCK {i} FAILED:\n{r.stderr}', file=sys.stderr)
    if not ok:
        sys.exit('build FAILED verification')

    kb = (ROOT / 'index.html').stat().st_size / 1024
    print(f'built index.html {kb:.0f}KB  cache {cache_version}')
    print(f"  Crazy Train hardest: beat {ct_hard['a']} ~16ths@{ct_hard['eq16']}")
    print(f"  Dolphin hardest: beat {ds_hard['a']} ~16ths@{ds_hard['eq16']}")
    print(f"  The World ({tw_bpm}bpm) hardest: beat {tw_hard['a']} ~16ths@{tw_hard['eq16']}")

if __name__ == '__main__':
    main()
