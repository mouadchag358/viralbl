"""Offline event-based soundtrack. Never replays the source MIDI timeline."""
import heapq
import subprocess
import wave
import numpy as np
import imageio_ffmpeg


def soundtrack(path, soundfont, pass_pcm, events, frames, fps, fluidsynth, gain):
    rate = 44100
    total = round(frames * rate / fps)
    synth = fluidsynth.Synth(gain=gain, samplerate=rate)
    sfid = synth.sfload(str(soundfont), update_midi_preset=0)
    if sfid < 0:
        synth.delete()
        raise RuntimeError('SoundFont illisible pendant export audio')
    pending = []
    serial = 0
    for when, kind, payload in events:
        heapq.heappush(pending, (max(0, round(when * rate)), serial, kind, payload))
        serial += 1
    voices = {}
    active_sfx = []
    position = 0
    try:
        with wave.open(str(path), 'wb') as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(rate)
            while position < total:
                while pending and pending[0][0] <= position:
                    _, _, kind, payload = heapq.heappop(pending)
                    if kind == 'midi':
                        event = payload
                        durations = event.durations or [event.duration] * len(event.notes)
                        for note, velocity, channel, program, duration in zip(
                                event.notes, event.velocities, event.channels, event.programs, durations):
                            synth.program_select(channel, sfid, 128 if channel == 9 else 0, program)
                            synth.noteoff(channel, note)
                            token = voices.get((channel, note), 0) + 1
                            voices[channel, note] = token
                            synth.noteon(channel, note, velocity)
                            heapq.heappush(pending, (position + round(duration * rate), serial,
                                                    'off', (channel, note, token)))
                            serial += 1
                    elif kind == 'off':
                        channel, note, token = payload
                        if voices.get((channel, note)) == token:
                            synth.noteoff(channel, note)
                    elif kind == 'pass':
                        active_sfx.append(position)
                    elif kind == 'reset':
                        for channel in range(16):
                            synth.cc(channel, 120, 0)
                        # Invalidate pending note-offs without reusing tokens.
                        voices = {key: token + 1 for key, token in voices.items()}
                end = min(total, position + 2048, pending[0][0] if pending else total)
                count = end - position
                if count <= 0:
                    continue
                pcm = np.asarray(synth.get_samples(count), dtype=np.int32).reshape(-1, 2)
                for start in active_sfx:
                    offset = position - start
                    length = min(count, len(pass_pcm) - offset)
                    if length > 0:
                        pcm[:length] += pass_pcm[offset:offset + length]
                active_sfx = [start for start in active_sfx if end - start < len(pass_pcm)]
                output.writeframes(np.clip(pcm, -32768, 32767).astype('<i2').tobytes())
                position = end
    finally:
        synth.delete()


def mux(video, audio, destination):
    result = subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), '-y', '-loglevel', 'error',
        '-i', str(video), '-i', str(audio), '-c:v', 'copy', '-c:a', 'aac',
        '-b:a', '256k', '-movflags', '+faststart', '-shortest', str(destination),
    ], capture_output=True, text=True,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('Assemblage audio/vidéo : ' + result.stderr)
