"""Windows MIDI -> keyboard player. Python 3.10+, stdlib only."""
from __future__ import annotations
import bisect
import ctypes as C
from ctypes import wintypes as W
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
import queue
import struct
import sys
import json
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

BASE = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent
WHITE = '1234567890qwertyuiopasdfghjklzxcvbnm'
SHIFTED = '!@#$%^&*()QWERTYUIOPASDFGHJKLZXCVBNM'
SCANS = dict(zip('1234567890', range(2, 12)))
SCANS.update(zip('qwertyuiop', range(16, 26)))
SCANS.update(zip('asdfghjkl', range(30, 39)))
SCANS.update(zip('zxcvbnm', range(44, 51)))
MAP = {}
for i, char in enumerate(WHITE):
    pitch = 36 + i // 7 * 12 + (0, 2, 4, 5, 7, 9, 11)[i % 7]
    MAP[pitch] = (SCANS[char], False, char)
    if pitch % 12 in (0, 2, 5, 7, 9) and pitch < 96:
        MAP[pitch + 1] = (SCANS[char], True, SHIFTED[i])


@dataclass
class Note:
    start: int
    end: int
    pitch: int
    velocity: int
    track: int
    channel: int


class Midi:
    def __init__(self, path):
        data = Path(path).read_bytes()
        if data[:4] != b'MThd' or len(data) < 14:
            raise ValueError('MIDIファイルではありません。')
        length = struct.unpack_from('>I', data, 4)[0]
        fmt, count, self.ppq = struct.unpack_from('>HHH', data, 8)
        if fmt not in (0, 1) or self.ppq & 0x8000 or not self.ppq:
            raise ValueError('形式0/1、拍単位のMIDIに対応しています。')
        p = 8 + length
        events = []
        for track in range(count):
            if data[p:p+4] != b'MTrk':
                raise ValueError('MIDIトラックが破損しています。')
            size = struct.unpack_from('>I', data, p+4)[0]
            p += 8
            end = p + size
            if end > len(data):
                raise ValueError('MIDIトラックが途中で切れています。')
            tick, running, order = 0, None, 0

            def vlq():
                nonlocal p
                value = 0
                for _ in range(4):
                    if p >= end:
                        raise ValueError('MIDIの可変長数値が破損しています。')
                    b = data[p]
                    p += 1
                    value = (value << 7) | (b & 127)
                    if not b & 128:
                        return value
                raise ValueError('MIDIの可変長数値が長すぎます。')

            while p < end:
                tick += vlq()
                status = data[p]
                if status & 128:
                    p += 1
                    if status < 240:
                        running = status
                elif running is not None:
                    status = running
                else:
                    raise ValueError('MIDIのランニングステータスが不正です。')
                if status == 255:
                    meta = data[p]
                    p += 1
                    n = vlq()
                    payload = data[p:p+n]
                    p += n
                    if meta == 81:
                        if len(payload) != 3 or int.from_bytes(payload, 'big') == 0:
                            raise ValueError('テンポ情報が不正です。')
                        events.append((tick, track, order, 'tempo', int.from_bytes(payload, 'big')))
                elif status in (240, 247):
                    n = vlq()
                    p += n
                    running = None
                elif status < 240:
                    kind, channel = status >> 4, status & 15
                    n = 1 if kind in (12, 13) else 2
                    payload = data[p:p+n]
                    p += n
                    if len(payload) != n or any(b > 127 for b in payload):
                        raise ValueError('MIDIイベントが不正です。')
                    events.append((tick, track, order, kind, channel, *payload))
                else:
                    raise ValueError('未対応のMIDIシステムイベントです。')
                if p > end:
                    raise ValueError('MIDIイベントがトラックを超えています。')
                order += 1
            events.append((tick, track, order, 'end'))
        events.sort(key=lambda e: e[:3])
        self.end_tick = max((e[0] for e in events), default=0)
        self.notes, self.pedals = [], []
        pending = defaultdict(deque)
        tempos = {0: 500000}
        for e in events:
            tick, track, _, kind = e[:4]
            if kind == 'tempo':
                tempos[tick] = e[4]
            elif kind in (8, 9):
                channel, pitch, velocity = e[4:7]
                if channel == 9:  # General MIDI percussion has no piano pitch mapping.
                    continue
                ident = (track, channel, pitch)
                if kind == 9 and velocity:
                    pending[ident].append((tick, velocity))
                elif pending[ident]:
                    start, vel = pending[ident].popleft()
                    self.notes.append(Note(start, tick, pitch, vel, track, channel))
            elif kind == 11 and e[5] == 64:
                self.pedals.append((tick, e[4], e[6]))
        for (track, channel, pitch), waiting in pending.items():
            for start, vel in waiting:
                self.notes.append(Note(start, self.end_tick, pitch, vel, track, channel))
        self.notes.sort(key=lambda n: (n.start, n.pitch))
        if not self.notes:
            raise ValueError('演奏できる音符がありません。')
        self.tempo_ticks, self.tempo_values, self.tempo_seconds = [], [], []
        total, previous_tick, previous_tempo = 0.0, 0, 500000
        for tick, tempo in sorted(tempos.items()):
            total += (tick-previous_tick) * previous_tempo / self.ppq / 1e6
            self.tempo_ticks.append(tick)
            self.tempo_values.append(tempo)
            self.tempo_seconds.append(total)
            previous_tick, previous_tempo = tick, tempo
        self.duration = self.seconds(self.end_tick)

    def seconds(self, tick):
        i = bisect.bisect_right(self.tempo_ticks, tick) - 1
        return self.tempo_seconds[i] + (tick-self.tempo_ticks[i]) * self.tempo_values[i] / self.ppq / 1e6


def schedule(midi, speed=1.0, mode='hold', sustain=True, transpose=0):
    """Create a deterministic event timeline without sending any input."""
    if not 0.25 <= speed <= 2.0 or not -24 <= transpose <= 24:
        raise ValueError('速度または移調が範囲外です。')
    result, folded = [], 0
    for ident, n in enumerate(midi.notes):
        pitch = n.pitch + transpose
        if pitch < 36 or pitch > 96:
            folded += 1
        while pitch < 36:
            pitch += 12
        while pitch > 96:
            pitch -= 12
        scan, shift, label = MAP[pitch]
        start, end = midi.seconds(n.start)/speed, midi.seconds(n.end)/speed
        if mode == 'tap':
            end = min(end, start + .045)
        end = max(end, start + .001)
        result.append((start, 1, ident, scan, shift, label))
        result.append((end, 0, ident, scan, shift, label))
    if sustain:
        channels, down = {}, False
        for tick, channel, value in midi.pedals:
            channels[channel] = value >= 64
            now = any(channels.values())
            if now != down:
                result.append((midi.seconds(tick)/speed, 2, -1, 57, now, 'pedal'))
                down = now
        if down:
            result.append((midi.duration/speed, 2, -1, 57, False, 'pedal'))
    result.sort(key=lambda e: (e[0], e[1], e[2]))
    return result, folded


class KEYBDINPUT(C.Structure):
    _fields_ = [('wVk', W.WORD), ('wScan', W.WORD), ('dwFlags', W.DWORD),
                ('time', W.DWORD), ('dwExtraInfo', C.c_size_t)]


class MOUSEINPUT(C.Structure):
    _fields_ = [('dx', W.LONG), ('dy', W.LONG), ('mouseData', W.DWORD),
                ('dwFlags', W.DWORD), ('time', W.DWORD), ('dwExtraInfo', C.c_size_t)]


class INPUTUNION(C.Union):
    _fields_ = [('ki', KEYBDINPUT), ('mi', MOUSEINPUT)]


class INPUT(C.Structure):
    _anonymous_ = ('u',)
    _fields_ = [('type', W.DWORD), ('u', INPUTUNION)]


class Keyboard:
    def __init__(self):
        self.user = C.WinDLL('user32', use_last_error=True)
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.user.SendInput.argtypes = [W.UINT, C.POINTER(INPUT), C.c_int]
        self.user.SendInput.restype = W.UINT
        self.user.GetForegroundWindow.restype = W.HWND
        self.user.GetAncestor.argtypes = [W.HWND, W.UINT]
        self.user.GetAncestor.restype = W.HWND
        self.user.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
        self.user.GetAsyncKeyState.argtypes = [C.c_int]
        self.user.GetAsyncKeyState.restype = C.c_short
        self.kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        self.kernel.OpenProcess.restype = W.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
        self.kernel.CloseHandle.argtypes = [W.HANDLE]
        self.held = set()

    def emergency(self):
        return bool(self.user.GetAsyncKeyState(0x77) & 0x8000)  # F8

    def modifiers_down(self):
        return any(self.user.GetAsyncKeyState(v) & 0x8000 for v in (16, 17, 18, 91, 92))

    def foreground_token(self):
        """Bind playback to a window and process, not just its editable title."""
        window = self.user.GetForegroundWindow()
        if not window:
            return None
        pid = W.DWORD()
        self.user.GetWindowThreadProcessId(window, C.byref(pid))
        return (window, pid.value)

    def root_window(self, window):
        return self.user.GetAncestor(window, 2) or window

    def foreground_is_roblox(self):
        pid = W.DWORD()
        self.user.GetWindowThreadProcessId(self.user.GetForegroundWindow(), C.byref(pid))
        handle = self.kernel.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return False
        try:
            size = W.DWORD(32768)
            name = C.create_unicode_buffer(size.value)
            ok = self.kernel.QueryFullProcessImageNameW(handle, 0, name, C.byref(size))
            return bool(ok and Path(name.value).name.lower() == 'robloxplayerbeta.exe')
        finally:
            self.kernel.CloseHandle(handle)

    def send(self, scan, down):
        event = INPUT(type=1)
        event.ki = KEYBDINPUT(0, scan, 0x0008 | (0 if down else 0x0002), 0, 0)
        if down:
            self.held.add(scan)  # Track even a partially successful failed send for cleanup.
        if self.user.SendInput(1, C.byref(event), C.sizeof(INPUT)) != 1:
            raise OSError('キー入力を送れませんでした。演奏先と本プログラムを通常権限で起動してください。')
        if not down:
            self.held.discard(scan)

    def key(self, scan, shift, down):
        # Explicit short Shift windows let adjacent white/black note-ons use their own state.
        if shift:
            self.send(42, True)
        try:
            self.send(scan, down)
        finally:
            if shift:
                self.send(42, False)

    def release_all(self):
        errors = []
        for scan in list(self.held):
            try:
                self.send(scan, False)
            except OSError as exc:
                errors.append(str(exc))
        return errors


class Player:
    def __init__(self, keyboard, stop, report, target_mode='any', excluded_window=None):
        self.keyboard, self.stop, self.report = keyboard, stop, report
        self.target_mode = target_mode
        self.excluded_window = excluded_window
        self.target_token = None

    def capture_target(self):
        token = self.keyboard.foreground_token()
        if not token or token[0] == self.excluded_window:
            raise InterruptedError('演奏先の画面へ切り替えてから開始してください。')
        if self.target_mode == 'roblox' and not self.keyboard.foreground_is_roblox():
            raise InterruptedError('Roblox限定モードです。Robloxを前面にしてください。')
        self.target_token = token

    def check(self, focus=True):
        if self.stop.is_set() or self.keyboard.emergency():
            raise InterruptedError('停止しました。')
        if focus and (self.target_token is None or self.keyboard.foreground_token() != self.target_token):
            raise InterruptedError('演奏先から画面が切り替わったため停止しました。')

    def wait_until(self, when, focus=True):
        while True:
            self.check(focus)
            remaining = when - time.perf_counter()
            if remaining <= 0:
                return
            self.stop.wait(min(remaining, .004))

    def run(self, events, countdown, duration):
        active = defaultdict(dict)
        try:
            deadline = time.perf_counter() + countdown
            while time.perf_counter() < deadline:
                self.report(f'{max(1, int(deadline-time.perf_counter())+1)}秒後に開始 — 演奏先のピアノ画面をクリック')
                self.wait_until(min(deadline, time.perf_counter()+.1), focus=False)
            self.check(focus=False)
            self.capture_target()
            self.check()
            if self.keyboard.modifiers_down():
                raise InterruptedError('Shift / Ctrl / Alt / Windowsキーを離してから開始してください。')
            start = time.perf_counter()
            last_update = -1
            for at, kind, ident, scan, shift, label in events:
                self.wait_until(start + at)
                if int(at) != last_update:
                    self.report(f'演奏中 {int(at)//60}:{int(at)%60:02d} / {int(duration)//60}:{int(duration)%60:02d} — F8で停止')
                    last_update = int(at)
                if kind == 1:
                    # Rearticulate repeated pitches; one physical key cannot hold two independent voices.
                    if active[scan]:
                        previous_shift = next(iter(active[scan].values()))
                        self.keyboard.key(scan, previous_shift, False)
                    active[scan][ident] = shift
                    self.keyboard.key(scan, shift, True)
                elif kind == 0:
                    active[scan].pop(ident, None)
                    if not active[scan]:
                        self.keyboard.key(scan, shift, False)
                else:
                    self.keyboard.send(scan, shift)
            self.wait_until(start + max(duration, events[-1][0] if events else 0))
            self.report('演奏が終わりました。')
        except (InterruptedError, OSError) as exc:
            self.report(str(exc))
        except Exception as exc:
            self.report(f'エラー: {exc}')
        finally:
            errors = self.keyboard.release_all()
            if errors:
                self.report('キーの解放に失敗しました。押されたままのキーを手動で押して離してください。')


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('MIDI キーボード演奏 — 汎用版')
        self.root.geometry('700x550')
        self.root.minsize(660, 520)
        self.root.configure(padx=22, pady=18)
        self.keyboard = Keyboard()
        self.stop = threading.Event()
        self.messages = queue.Queue()
        self.worker = None
        self.midi = None
        self.file = tk.StringVar(value=str(BASE/'samples'/'demo.mid'))
        self.speed = tk.StringVar(value='100')
        self.countdown = tk.StringVar(value='5')
        self.transpose = tk.StringVar(value='0')
        self.mode = tk.StringVar(value='hold')
        self.target_mode = tk.StringVar(value='前面のアプリ / ブラウザー')
        self.sustain = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value='MIDIを選んで開始してください。')
        ttk.Label(self.root, text='MIDI → キーボード演奏', font=('Yu Gothic UI', 19, 'bold')).pack(anchor='w')
        ttk.Label(self.root, text='演奏先を開く → 開始 → 待ち時間中に演奏先をクリック', padding=(0,8)).pack(anchor='w')
        target_row = ttk.Frame(self.root)
        target_row.pack(fill='x', pady=(4,8))
        ttk.Label(target_row, text='入力先  ').pack(side='left')
        ttk.Combobox(target_row, textvariable=self.target_mode, state='readonly', width=35,
                     values=('前面のアプリ / ブラウザー', 'Roblox限定')).pack(side='left')
        row = ttk.Frame(self.root)
        row.pack(fill='x', pady=8)
        ttk.Entry(row, textvariable=self.file).pack(side='left', fill='x', expand=True)
        self.browse = ttk.Button(row, text='MIDIを選ぶ', command=self.choose)
        self.browse.pack(side='right', padx=(8,0))
        self.info = ttk.Label(self.root, text='', wraplength=600)
        self.info.pack(anchor='w', pady=8)
        settings = ttk.Frame(self.root)
        settings.pack(fill='x', pady=10)
        for column, (label, variable, low, high) in enumerate([
            ('速度 (%)', self.speed, 25, 200), ('待ち時間 (秒)', self.countdown, 3, 30),
            ('移調 (半音)', self.transpose, -24, 24)]):
            ttk.Label(settings, text=label).grid(row=0,column=column,padx=(0,30),sticky='w')
            ttk.Spinbox(settings, from_=low, to=high, textvariable=variable, width=12).grid(row=1,column=column,padx=(0,30),sticky='w')
        ttk.Radiobutton(self.root, text='原音の長さで押す', variable=self.mode, value='hold').pack(anchor='w')
        ttk.Radiobutton(self.root, text='短く押す（音が残る場合はこちら）', variable=self.mode, value='tap').pack(anchor='w')
        ttk.Checkbutton(self.root, text='MIDIのペダルをSpaceキーで送る', variable=self.sustain).pack(anchor='w',pady=5)
        buttons = ttk.Frame(self.root)
        buttons.pack(fill='x', pady=12)
        self.start_button = ttk.Button(buttons,text='開始',command=self.start)
        self.start_button.pack(side='left',ipadx=25)
        ttk.Button(buttons,text='停止 (F8)',command=self.stop.set).pack(side='left',padx=10,ipadx=15)
        ttk.Label(self.root,textvariable=self.status,wraplength=600,foreground='#125a80').pack(anchor='w',pady=5)
        ttk.Label(self.root,text='標準Virtual Pianoのキー配置に対応。演奏先の転調は0、IMEは英数。\n開始時のウィンドウへ入力します。別ウィンドウへ切り替えると自動停止。\nブラウザー内のタブ切り替え・入力欄の移動は検出できません。',wraplength=650).pack(anchor='w',pady=6)
        self.root.protocol('WM_DELETE_WINDOW',self.close)
        self.root.after(50,self.poll)
        if Path(self.file.get()).is_file():
            self.load()

    def choose(self):
        name=filedialog.askopenfilename(filetypes=[('MIDI','*.mid *.midi'),('All files','*.*')])
        if name:
            self.file.set(name)
            self.load()

    def load(self):
        try:
            self.midi=Midi(self.file.get())
            _, folded=schedule(self.midi)
            d=int(self.midi.duration)
            self.info.configure(text=f'{len(self.midi.notes):,}音 / {d//60}分{d%60:02d}秒 / 音域外{folded}音をオクターブ補正')
        except Exception as exc:
            self.midi=None
            self.info.configure(text='読み込み失敗')
            messagebox.showerror('MIDIを読み込めません',str(exc))

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.load()
        if not self.midi:
            return
        try:
            speed=float(self.speed.get())/100
            countdown=float(self.countdown.get())
            transpose=int(self.transpose.get())
            if not 3 <= countdown <= 30:
                raise ValueError('待ち時間は3〜30秒にしてください。')
            events, folded=schedule(self.midi,speed,self.mode.get(),self.sustain.get(),transpose)
        except ValueError as exc:
            messagebox.showerror('設定を確認',str(exc))
            return
        self.info.configure(text=f'{len(self.midi.notes):,}音 / オクターブ補正{folded}音 / 速度{speed*100:g}%')
        self.stop.clear()
        self.start_button.configure(state='disabled')
        self.browse.configure(state='disabled')
        target_mode = 'roblox' if self.target_mode.get() == 'Roblox限定' else 'any'
        excluded_window = self.keyboard.root_window(self.root.winfo_id())
        player=Player(self.keyboard,self.stop,self.messages.put,target_mode,excluded_window)
        self.worker=threading.Thread(target=player.run,args=(events,countdown,self.midi.duration/speed),daemon=True)
        self.worker.start()

    def poll(self):
        while not self.messages.empty():
            self.status.set(self.messages.get_nowait())
        if self.worker and not self.worker.is_alive():
            self.start_button.configure(state='normal')
            self.browse.configure(state='normal')
        self.root.after(50,self.poll)

    def close(self):
        self.stop.set()
        if self.worker and self.worker.is_alive():
            self.root.after(30,self.close)
        else:
            self.keyboard.release_all()
            self.root.destroy()


def self_test(output):
    """Check the packaged runtime without sending keyboard input."""
    midi = Midi(BASE/'samples'/'demo.mid')
    events, folded = schedule(midi)
    keyboard = Keyboard()
    assert len(midi.notes) == 8 and len(events) == 16 and folded == 0
    assert C.sizeof(INPUT) == (40 if C.sizeof(C.c_void_p) == 8 else 28)
    root = tk.Tk()
    root.withdraw()
    root.update_idletasks()
    root.destroy()
    Path(output).write_text(json.dumps({'result': 'PASS', 'notes': len(midi.notes),
        'events': len(events), 'tk': tk.TkVersion, 'frozen': bool(getattr(sys, 'frozen', False)),
        'held_keys': len(keyboard.held)}, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--self-test':
        self_test(sys.argv[2])
    else:
        App().root.mainloop()
