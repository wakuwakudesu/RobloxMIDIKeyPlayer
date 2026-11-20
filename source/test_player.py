"""Read-only/dry-run tests. No actual keyboard input is sent."""
import ctypes
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from player import Midi, MAP, schedule, Player, Keyboard, INPUT


def vlq(n):
    out = [n & 127]
    n >>= 7
    while n:
        out.insert(0, 128 | (n & 127))
        n >>= 7
    return bytes(out)


def midi_bytes(track):
    return b'MThd'+struct.pack('>IHHH', 6, 0, 1, 480)+b'MTrk'+struct.pack('>I', len(track))+track


class FakeKeyboard:
    def __init__(self, focus=True, emergency=False, fail=False):
        self.focus, self.panic, self.fail = focus, emergency, fail
        self.calls = []
        self.cleaned = False
        self.token = (100, 200) if focus else None
    def emergency(self): return self.panic
    def modifiers_down(self): return False
    def foreground_is_roblox(self): return self.focus
    def foreground_token(self): return self.token
    def key(self, scan, shift, down):
        self.calls.append((scan, shift, down))
        if self.fail: raise OSError('simulated failure')
    def send(self, scan, down): self.calls.append((scan, False, down))
    def release_all(self):
        self.cleaned = True
        return []


class Tests(unittest.TestCase):
    def test_bundled_midi(self):
        fixture = Path(__file__).resolve().parent.parent/'個人利用MIDI'/'INMU_KING_piano.mid'
        if not fixture.is_file():
            self.skipTest('個人利用MIDIは配布時に除外できます。')
        m = Midi(fixture)
        self.assertEqual(len(m.notes), 2425)
        self.assertEqual(m.ppq, 480)
        self.assertEqual((min(n.pitch for n in m.notes),max(n.pitch for n in m.notes)),(29,89))
        self.assertAlmostEqual(m.duration,221.503848)
        events, folded = schedule(m)
        self.assertEqual(folded,138)
        self.assertEqual(sum(e[1] == 1 for e in events),2425)
        self.assertEqual(sum(e[1] == 0 for e in events),2425)
        self.assertEqual(events,sorted(events,key=lambda e:e[:3]))
        slow,_=schedule(m,speed=.5)
        self.assertAlmostEqual(max(e[0] for e in slow),max(e[0] for e in events)*2)

    def test_mapping(self):
        self.assertEqual(len(MAP),61)
        self.assertEqual(MAP[36][2],'1')
        self.assertEqual(MAP[60][2],'t')
        self.assertEqual(MAP[61][2],'T')
        self.assertEqual(MAP[96][2],'m')
        self.assertEqual(ctypes.sizeof(INPUT),40 if ctypes.sizeof(ctypes.c_void_p)==8 else 28)

    def test_tempo_running_status_and_velocity_zero(self):
        track=(b'\x00\xff\x51\x03\x07\xa1\x20'+b'\x00\x90\x3c\x64'
               +vlq(480)+b'\x3c\x00'+b'\x00\xff\x51\x03\x0f\x42\x40'
               +b'\x00\x90\x40\x64'+vlq(480)+b'\x80\x40\x40'+b'\x00\xff\x2f\x00')
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'test.mid';p.write_bytes(midi_bytes(track));m=Midi(p)
        self.assertEqual(len(m.notes),2)
        self.assertAlmostEqual(m.seconds(480),.5)
        self.assertAlmostEqual(m.duration,1.5)

    def test_reject_truncated_file(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bad.mid';p.write_bytes(midi_bytes(b'\x00\x90\x3c\x64')[:-1])
            with self.assertRaises(ValueError): Midi(p)

    def test_stop_focus_and_f8_send_no_notes(self):
        for kwargs in ({'focus':False},{'emergency':True}):
            k=FakeKeyboard(**kwargs)
            Player(k,threading.Event(),lambda s:None).run([(0,1,0,16,False,'q')],0,0)
            self.assertEqual(k.calls,[])
            self.assertTrue(k.cleaned)

    def test_release_on_error_and_normal_play(self):
        events=[(0,1,0,16,False,'q'),(.001,0,0,16,False,'q')]
        for fail in (False,True):
            k=FakeKeyboard(fail=fail)
            Player(k,threading.Event(),lambda s:None).run(events,0,.001)
            self.assertTrue(k.cleaned)
            self.assertTrue(k.calls)
        self.assertEqual(k.calls[0],(16,False,True))

    def test_cancel_countdown(self):
        k=FakeKeyboard();stop=threading.Event();stop.set()
        started=time.perf_counter()
        Player(k,stop,lambda s:None).run([],5,0)
        self.assertLess(time.perf_counter()-started,.1)
        self.assertTrue(k.cleaned)
        self.assertFalse(k.calls)

    def test_api_initialization_without_sending(self):
        k=Keyboard()
        self.assertIsInstance(k.foreground_is_roblox(),bool)
        token=k.foreground_token()
        self.assertTrue(token is None or len(token)==2)
        self.assertFalse(k.held)

    def test_generic_mode_accepts_non_roblox(self):
        k=FakeKeyboard()
        k.foreground_is_roblox=lambda:False
        Player(k,threading.Event(),lambda s:None).run(
            [(0,1,0,16,False,'q'),(.001,0,0,16,False,'q')],0,.001)
        self.assertEqual(len(k.calls),2)
        self.assertTrue(k.cleaned)

    def test_roblox_only_rejects_other_apps(self):
        k=FakeKeyboard()
        k.foreground_is_roblox=lambda:False
        Player(k,threading.Event(),lambda s:None,target_mode='roblox').run(
            [(0,1,0,16,False,'q')],0,0)
        self.assertFalse(k.calls)
        self.assertTrue(k.cleaned)

    def test_reject_own_window(self):
        k=FakeKeyboard()
        Player(k,threading.Event(),lambda s:None,excluded_window=100).run(
            [(0,1,0,16,False,'q')],0,0)
        self.assertFalse(k.calls)

    def test_window_change_stops_without_new_keydowns(self):
        k=FakeKeyboard()
        original=k.key
        def switch_after_first_note(scan,shift,down):
            original(scan,shift,down)
            k.token=(101,201)
        k.key=switch_after_first_note
        Player(k,threading.Event(),lambda s:None).run(
            [(0,1,0,16,False,'q'),(.001,1,1,17,False,'w')],0,.001)
        self.assertEqual(k.calls,[(16,False,True)])
        self.assertTrue(k.cleaned)

    def test_same_handle_different_process_is_rejected(self):
        k=FakeKeyboard()
        player=Player(k,threading.Event(),lambda s:None)
        player.capture_target()
        k.token=(100,201)
        with self.assertRaises(InterruptedError): player.check()


if __name__=='__main__': unittest.main(verbosity=2)
