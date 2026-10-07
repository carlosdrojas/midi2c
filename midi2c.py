#!/usr/bin/env python3
"""Convert a MIDI file into sw/src/song.c for the Lab 5 music player.

No packages needed, only the Python standard library.

Typical use:
  1. In MuseScore, keep one melody staff and one bass staff, cut the song
     to the section you want, and export as .mid.
  2. List the tracks to see which is which:
       python3 tools/midi2c.py song.mid --list
  3. Generate song.c:
       python3 tools/midi2c.py song.mid --melody 1 --bass 2 \
           --title "MY EYES (bridge)" -o sw/src/song.c

Options worth knowing:
  --start-bar / --end-bar   keep only part of the song (bars counted from 1)
  --beats-per-bar           default 4 (4/4 time)
  --bpm                     override the tempo stored in the file
  --transpose-melody / --transpose-bass   shift by semitones (12 = one octave)

Notes are rounded to the nearest 16th note. If a staff has chords, the melody
keeps the highest note and the bass keeps the lowest. Notes outside C2-B6 are
moved by octaves into range. The JitterTest song is always written too.

If both hands are in one track, use the same track for both voices and
--split to divide them by pitch, e.g. --melody 0 --bass 0 --split E4.
If every note is a short blip (common in online-sequencer exports), add
--legato so each note rings until the next one.
"""
import argparse
import re
import struct
import sys

NAMES = ['C', 'CS', 'D', 'DS', 'E', 'F', 'FS', 'G', 'GS', 'A', 'AS', 'B']
LOW, HIGH = 36, 95  # C2 .. B6, the range of Pitch_t in music.h


def name(midi):
    return NAMES[midi % 12] + str(midi // 12 - 1)


def read_vlq(data, i):
    value = 0
    while True:
        b = data[i]
        i += 1
        value = (value << 7) | (b & 0x7F)
        if b < 0x80:
            return value, i


def parse_midi(path):
    """Return (ticks_per_quarter, tempo_us_per_quarter or None, tracks).
    Each track is a dict with 'name' and 'notes' = [(start, end, midi), ...]."""
    data = open(path, 'rb').read()
    if data[:4] != b'MThd':
        sys.exit(f'{path} is not a standard MIDI file')
    hlen = struct.unpack('>I', data[4:8])[0]
    _fmt, ntracks, division = struct.unpack('>HHH', data[8:14])
    if division & 0x8000:
        sys.exit('SMPTE time division is not supported; re-export from MuseScore')
    i = 8 + hlen
    tempo = None
    tracks = []
    for _ in range(ntracks):
        if data[i:i + 4] != b'MTrk':
            sys.exit('bad track header')
        tlen = struct.unpack('>I', data[i + 4:i + 8])[0]
        j, end = i + 8, i + 8 + tlen
        tick, status = 0, 0
        on = {}  # (channel, note) -> [start ticks]
        notes, tname = [], ''
        while j < end:
            delta, j = read_vlq(data, j)
            tick += delta
            b = data[j]
            if b == 0xFF:  # meta event
                mtype = data[j + 1]
                mlen, k = read_vlq(data, j + 2)
                body = data[k:k + mlen]
                if mtype == 0x51 and tempo is None:
                    tempo = (body[0] << 16) | (body[1] << 8) | body[2]
                elif mtype == 0x03 and not tname:
                    tname = body.decode('latin-1', 'replace')
                j = k + mlen
                continue
            if b in (0xF0, 0xF7):  # sysex
                slen, k = read_vlq(data, j + 1)
                j = k + slen
                continue
            if b & 0x80:
                status = b
                j += 1
            kind, ch = status & 0xF0, status & 0x0F
            if kind in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                d1, d2 = data[j], data[j + 1]
                j += 2
                if kind == 0x90 and d2 > 0:
                    on.setdefault((ch, d1), []).append(tick)
                elif kind == 0x80 or (kind == 0x90 and d2 == 0):
                    starts = on.get((ch, d1))
                    if starts:
                        notes.append((starts.pop(0), tick, d1))
            else:  # 0xC0 program change, 0xD0 channel pressure
                j += 1
        tracks.append({'name': tname, 'notes': sorted(notes)})
        i = end
    return division, tempo, tracks


def parse_note(text):
    """'E4', 'FS4' or 'F#4' -> MIDI number"""
    m = re.fullmatch(r'([A-Ga-g])(S|s|#)?(-?\d)', text.strip())
    if not m:
        sys.exit(f'bad note name {text!r}, use something like E4, FS4 or F#4')
    return (int(m.group(3)) + 1) * 12 + NAMES.index(m.group(1).upper() + ('S' if m.group(2) else ''))


def pitch_range(notes, low=None, high=None):
    """keep notes with low <= midi < high"""
    return [(s, e, m) for s, e, m in notes
            if (low is None or m >= low) and (high is None or m < high)]


def legato(notes):
    """hold every note until the next note starts in the same voice"""
    onsets = sorted({s for s, _, _ in notes})
    nxt = {a: b for a, b in zip(onsets, onsets[1:])}
    return [(s, max(e, nxt.get(s, e)), m) for s, e, m in notes]


def to_voice(notes, tpq, start_q, end_q, transpose, pick_high):
    """Quantize to 16ths and reduce to one note at a time.
    Returns a list of (midi or None for rest, sixteenths)."""
    t16 = tpq / 4
    slot = {}  # 16th index -> (midi, note id)
    for nid, (s, e, m) in enumerate(notes):
        qs, qe = round(s / t16), round(e / t16)
        if qe <= qs:
            qe = qs + 1
        m += transpose
        while m < LOW:
            m += 12
        while m > HIGH:
            m -= 12
        for q in range(max(qs, start_q), min(qe, end_q)):
            cur = slot.get(q)
            if cur is None or (m > cur[0] if pick_high else m < cur[0]):
                slot[q] = (m, nid)
    out = []
    prev = 'start'
    for q in range(start_q, end_q):
        cur = slot.get(q)
        if out and cur == prev:
            out[-1][1] += 1
        else:
            out.append([cur[0] if cur else None, 1])
        prev = cur
    while out and out[-1][0] is None:  # drop trailing rest
        out.pop()
    split = []  # Note_t.sixteenths is a uint8_t, max 255
    for m, d in out:
        while d > 255:
            split.append((m, 255))
            d -= 255
        split.append((m, d))
    return split


def c_notes(var, voice):
    lines, row = [], []
    for m, d in voice:
        row.append('{%s,%d}' % (name(m) if m is not None else 'REST', d))
        if len(row) == 8:
            lines.append('  ' + ','.join(row) + ',')
            row = []
    if row:
        lines.append('  ' + ','.join(row) + ',')
    lines.append('  {END,0}')
    return 'static const Note_t %s[] = {\n%s\n};\n' % (var, '\n'.join(lines))


JITTER = '''
// Jitter test: the melody holds A4 for about a minute while the bass
// keeps changing, so all three interrupts are active.
static const Note_t JitterMelody[] = {
  {A4,255},{A4,255},{A4,255},{A4,255},
  {END,0}
};
static const Note_t JitterBass[] = {
  {C3,16},{E3,16},{G3,16},{C4,16},{G3,16},{E3,16},{C3,16},{G2,16},
  {C3,16},{E3,16},{G3,16},{C4,16},{G3,16},{E3,16},{C3,16},{G2,16},
  {C3,16},{E3,16},{G3,16},{C4,16},{G3,16},{E3,16},{C3,16},{G2,16},
  {C3,16},{E3,16},{G3,16},{C4,16},{G3,16},{E3,16},{C3,16},{G2,16},
  {END,0}
};
const Song_t JitterTest = {"Jitter test", 120, JitterMelody, JitterBass};
'''


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('midi')
    ap.add_argument('--list', action='store_true', help='list tracks and exit')
    ap.add_argument('--melody', type=int, help='track number for the melody')
    ap.add_argument('--bass', type=int, help='track number for the bass (optional)')
    ap.add_argument('--title', default='Song')
    ap.add_argument('--bpm', type=int)
    ap.add_argument('--start-bar', type=int, default=1)
    ap.add_argument('--end-bar', type=int, help='last bar to keep (inclusive)')
    ap.add_argument('--beats-per-bar', type=int, default=4)
    ap.add_argument('--transpose-melody', type=int, default=0)
    ap.add_argument('--transpose-bass', type=int, default=0)
    ap.add_argument('--split', help='note like E4: melody keeps notes at or above it, bass keeps notes below it')
    ap.add_argument('--legato', action='store_true', help='hold each note until the next one in its voice')
    ap.add_argument('-o', '--output', default='-')
    a = ap.parse_args()

    tpq, tempo, tracks = parse_midi(a.midi)
    bpm = a.bpm or (round(60_000_000 / tempo) if tempo else 120)

    if a.list or a.melody is None:
        print(f'{a.midi}: {len(tracks)} tracks, tempo {bpm} BPM')
        for n, t in enumerate(tracks):
            ns = t['notes']
            if ns:
                lo, hi = min(m for _, _, m in ns), max(m for _, _, m in ns)
                last_bar = max(e for _, e, _ in ns) / tpq / a.beats_per_bar
                print(f'  track {n}: {len(ns):4d} notes, {name(lo)}-{name(hi)}, '
                      f'about {last_bar:.0f} bars  {t["name"]}')
            else:
                print(f'  track {n}:    0 notes  {t["name"]}')
        if a.melody is None and not a.list:
            print('\nChoose tracks with --melody N (and --bass M).')
        return

    per_bar = a.beats_per_bar * 4
    start_q = (a.start_bar - 1) * per_bar
    all_end = max((e for t in tracks for _, e, _ in t['notes']), default=0)
    end_q = a.end_bar * per_bar if a.end_bar else round(all_end / (tpq / 4))

    split = parse_note(a.split) if a.split else None
    mel_notes = pitch_range(tracks[a.melody]['notes'], low=split)
    bas_notes = pitch_range(tracks[a.bass]['notes'], high=split) if a.bass is not None else None
    if a.legato:
        mel_notes = legato(mel_notes)
        bas_notes = legato(bas_notes) if bas_notes is not None else None
    mel = to_voice(mel_notes, tpq, start_q, end_q, a.transpose_melody, True)
    bas = to_voice(bas_notes, tpq, start_q, end_q, a.transpose_bass, False) if bas_notes is not None else None

    sixteenths = sum(d for _, d in mel)
    seconds = sixteenths * 15 / bpm
    pitches = {m for m, _ in mel + (bas or []) if m is not None}

    src = ['// File **********song.c***********',
           '// Lab 5',
           f'// Generated by tools/midi2c.py from {a.midi.split("/")[-1]}',
           f'// {a.title}: {bpm} BPM, bars {a.start_bar}-{a.end_bar or "end"}, '
           f'about {seconds:.0f} seconds, {len(pitches)} different pitches.',
           '// Durations are in 16th notes: quarter = 4, eighth = 2, half = 8.',
           '// ECE445L Fall 2026',
           '',
           '#include "music.h"',
           '#include "song.h"',
           '',
           c_notes('Melody', mel)]
    if bas is not None:
        src.append(c_notes('Bass', bas))
    title = a.title.replace('"', "'")
    src.append('const Song_t MainSong = {"%s", %d, Melody, %s};' % (title, bpm, 'Bass' if bas is not None else '0'))
    src.append(JITTER)
    text = '\n'.join(src)
    if a.output == '-':
        print(text)
    else:
        open(a.output, 'w').write(text)
    print(f'{a.title}: {len(mel)} melody notes, {len(bas) if bas else 0} bass notes, '
          f'{bpm} BPM, about {seconds:.0f} s, {len(pitches)} pitches', file=sys.stderr)
    if seconds < 60:
        print('warning: shorter than the 60 s the lab requires', file=sys.stderr)
    if len(pitches) < 8:
        print('warning: fewer than 8 different pitches', file=sys.stderr)


if __name__ == '__main__':
    main()
