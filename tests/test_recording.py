import os
os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch
from collections import defaultdict
import numpy as np
import main
from recording_audio import soundtrack


class AudioTests(unittest.TestCase):
    def test_older_note_off_does_not_cut_repeated_note(self):
        synth = main.MidiSynth.__new__(main.MidiSynth)
        synth.synth = Mock()
        synth.sfid = 1
        synth.muted = False
        synth.channel_program = {}
        synth.pending_offs = []
        synth.note_tokens = defaultdict(int)
        event = main.MidiEvent([60], [90], .2, [0], [0], [.2])
        with patch('main.time.monotonic', return_value=0):
            synth.play_chord(event)
        with patch('main.time.monotonic', return_value=.1):
            synth.play_chord(event)
        synth.synth.noteoff.reset_mock()
        with patch('main.time.monotonic', return_value=.21):
            synth.update()
        synth.synth.noteoff.assert_not_called()
        with patch('main.time.monotonic', return_value=.31):
            synth.update()
        synth.synth.noteoff.assert_called_once_with(0, 60)

    def test_soundtrack_places_pass_on_exact_frame(self):
        native = Mock()
        native.Synth.return_value.sfload.return_value = 1
        native.Synth.return_value.get_samples.side_effect = lambda n: np.zeros(n * 2, dtype=np.int16)
        sfx = np.full((441, 2), 5000, dtype=np.int32)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sound.wav'
            soundtrack(path, 'test.sf2', sfx, [(0.1, 'pass', None)], 12, 60, native, .8)
            with wave.open(str(path), 'rb') as f:
                self.assertEqual(f.getnframes(), 8820)
                samples = np.frombuffer(f.readframes(f.getnframes()), dtype='<i2').reshape(-1, 2)
            self.assertTrue(np.all(samples[:4410] == 0))
            self.assertTrue(np.all(samples[4410:4851] == 5000))
            self.assertTrue(np.all(samples[4851:] == 0))

    def test_failed_encoder_cannot_block_full_queue(self):
        recorder = main.VideoRecorder()
        recorder._thread = Mock()
        recorder._thread.is_alive.return_value = False
        recorder.error = 'injected failure'
        with self.assertRaisesRegex(RuntimeError, 'injected failure'):
            recorder._enqueue(b'frame')


if __name__ == '__main__':
    unittest.main()
