"""
Utility functions and constants for the Fishing Bot
"""

import os
import sys
import threading
import winsound
import ctypes
import time

# Thread synchronization for mouse/keyboard - prevents race conditions
input_lock = threading.Lock()

# Max simultaneous game windows
MAX_WINDOWS = 8

# Debug mode - enable/disable IgnoredPositionsWindow
DEBUG_MODE_EN = False

# Debug prints - enable/disable verbose debug print statements
DEBUG_PRINTS = False


def is_running_as_admin() -> bool:
    """Returns True when the current process is elevated on Windows."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def send_scan_key(key: str, hold_seconds: float = 0.025) -> bool:
    """Sends a keyboard key using WinAPI SendInput scancodes."""
    scan_codes = {
        '1': 0x02, '2': 0x03, '3': 0x04, '4': 0x05,
        'f1': 0x3B, 'f2': 0x3C, 'f3': 0x3D, 'f4': 0x3E,
        'space': 0x39,
        'q': 0x10, 'e': 0x12, 'r': 0x13, 'f': 0x21,
        'g': 0x22,
        'l': 0x26,
        'ctrl': 0x1D,
    }
    scan = scan_codes.get(str(key).lower())
    if scan is None:
        return False

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_long),
            ("dy", ctypes.c_long),
            ("mouseData", ctypes.c_ulong),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_ushort),
            ("wScan", ctypes.c_ushort),
            ("dwFlags", ctypes.c_ulong),
            ("time", ctypes.c_ulong),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", ctypes.c_ulong),
            ("wParamL", ctypes.c_ushort),
            ("wParamH", ctypes.c_ushort),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("union", INPUT_UNION)]

    INPUT_KEYBOARD = 1
    KEYEVENTF_SCANCODE = 0x0008
    KEYEVENTF_KEYUP = 0x0002
    ctypes.set_last_error(0)
    user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = ctypes.c_uint

    def _input(flags):
        item = INPUT()
        item.type = INPUT_KEYBOARD
        item.union.ki = KEYBDINPUT(0, scan, flags, 0, 0)
        return item

    down = _input(KEYEVENTF_SCANCODE)
    up = _input(KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP)
    sent = user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
    if sent != 1:
        return False
    time.sleep(max(0.0, hold_seconds))
    sent = user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
    return sent == 1


def get_last_win_error() -> int:
    """Returns the last WinAPI error captured through ctypes."""
    try:
        return ctypes.get_last_error()
    except Exception:
        return 0


def get_resource_path(filename: str) -> str:
    """Get the path to a bundled resource (works both in dev and in PyInstaller exe)
    
    Assets like images (.gif, .ico, .jpg, .png) are looked up in the assets folder.
    """
    if hasattr(sys, '_MEIPASS'):
        # Running as PyInstaller bundle
        # PyInstaller extracts assets to _MEIPASS/assets/ based on build.spec configuration
        if filename.endswith(('.gif', '.ico', '.jpg', '.png')) or filename == 'assets':
            # Asset files go in assets subfolder
            return os.path.join(sys._MEIPASS, 'assets', filename) if filename != 'assets' else os.path.join(sys._MEIPASS, 'assets')
        else:
            # Other files go in root
            return os.path.join(sys._MEIPASS, filename)
    else:
        # Running as script - check if file should be in assets folder
        base_dir = os.path.dirname(os.path.dirname(__file__))  # Go up from src to project root
        
        # Check if it's an asset file (images, icons, etc.)
        if filename.endswith(('.gif', '.ico', '.jpg', '.png')) or filename == 'assets':
            return os.path.join(base_dir, 'assets', filename) if filename != 'assets' else os.path.join(base_dir, 'assets')
        
        # For other files, check assets folder first
        assets_path = os.path.join(base_dir, 'assets', filename)
        if os.path.exists(assets_path):
            return assets_path
        
        # Default: look in project root
        return os.path.join(base_dir, filename)


def set_window_icon(window, icon_path: str):
    """Sets window icon using multiple methods for maximum compatibility.
    
    This function ensures the icon appears in both the window title bar and Windows taskbar.
    Uses both iconbitmap() and iconphoto() for better PyInstaller compatibility.
    
    Args:
        window: Tkinter window (Tk or Toplevel)
        icon_path: Path to .ico file
    """
    if not os.path.exists(icon_path):
        return
    
    try:
        # Method 1: Use iconbitmap for title bar (Windows-specific)
        window.iconbitmap(icon_path)
        window.iconbitmap(default=icon_path)
    except Exception:
        pass
    
    try:
        # Method 2: Use iconphoto for taskbar (cross-platform, more reliable with PyInstaller)
        # Convert .ico to PhotoImage using PIL
        from PIL import Image, ImageTk
        
        # Load .ico and convert to PhotoImage
        icon_image = Image.open(icon_path)
        # Use the largest size available in the .ico
        if hasattr(icon_image, 'size'):
            photo = ImageTk.PhotoImage(icon_image)
            window.iconphoto(True, photo)
            # Keep a reference to prevent garbage collection
            window._icon_photo = photo
    except Exception:
        pass


def load_window_icon(window) -> None:
    """Applies the app icon to a Tk Toplevel window. Silently skips if missing."""
    set_window_icon(window, get_resource_path("ac_valhalla.ico"))


def play_rickroll_beep():
    """Plays a Rick Roll-themed beep sequence with smooth ADSR envelopes.
    Uses numpy to generate WAV audio with professional envelope curves."""
    try:
        import numpy as np
        import io
        import wave
        import tempfile
        import time
        
        melody = [
            (554, 600),   # C5s - strong opening
            (622, 1000),  # E5f - longer note
            (622, 600),   # E5f
            (698, 600),   # F5
            (831, 100),   # A5f - quick notes
            (740, 100),   # F5s
            (698, 100),   # F5
            (622, 100),   # E5f
            (554, 600),   # C5s
            (622, 800),   # E5f - held note
            (415, 400),   # A4f - step down
            (415, 200),   # A4f
        ]
        
        sample_rate = 44100
        audio_data = []
        
        for frequency, duration in melody:
            # Generate samples for this note
            num_samples = int(sample_rate * duration / 1000)
            t = np.linspace(0, duration / 1000, num_samples, False)
            
            # Generate sine wave
            wave_data = np.sin(2.0 * np.pi * frequency * t)
            
            # ADSR Envelope (Attack, Decay, Sustain, Release)
            attack_ms = min(20, duration * 0.12)      # 20ms or 12% of note
            release_ms = min(40, duration * 0.25)     # 40ms or 25% of note
            
            attack_samples = max(int(sample_rate * attack_ms / 1000), 1)
            release_samples = max(int(sample_rate * release_ms / 1000), 1)
            
            envelope = np.ones(num_samples)
            
            # Attack: smooth fade in (exponential curve for musicality)
            if attack_samples < num_samples:
                envelope[:attack_samples] = (np.linspace(0, 1, attack_samples) ** 1.5)
            
            # Release: smooth fade out
            if release_samples < num_samples:
                envelope[-release_samples:] = (np.linspace(1, 0, release_samples) ** 1.5)
            
            # Apply envelope and volume
            wave_data = wave_data * envelope * 0.28  # 28% volume
            audio_data.extend(wave_data)
            
            # Add gap between notes (10ms)
            gap_samples = int(sample_rate * 0.01)
            audio_data.extend(np.zeros(gap_samples))
        
        # Convert to 16-bit PCM
        audio_array = np.array(audio_data, dtype=np.float32)
        audio_array = np.clip(audio_array, -1.0, 1.0)
        audio_int16 = (audio_array * 32767).astype(np.int16)
        
        # Create WAV in memory
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, 'wb') as wav_file:
            wav_file.setnchannels(1)      # Mono
            wav_file.setsampwidth(2)       # 16-bit
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_int16.tobytes())
        
        # Write to temp file and play
        wav_buffer.seek(0)
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
            tmp.write(wav_buffer.getvalue())
            tmp_path = tmp.name
        
        try:
            winsound.PlaySound(tmp_path, winsound.SND_FILENAME | winsound.SND_NODEFAULT)
        finally:
            time.sleep(0.1)  # Small delay before cleanup
            os.remove(tmp_path)
    
    except (ImportError, Exception):
        # Fallback to original winsound beeps if numpy not available
        import time
        melody = [
            (554, 600), (622, 1000), (622, 600), (698, 600),
            (831, 100), (740, 100), (698, 100), (622, 100),
            (554, 600), (622, 800), (415, 400), (415, 200),
        ]
        for frequency, duration in melody:
            winsound.Beep(frequency, duration)
            time.sleep(0.01)  # Small gap between notes
