"""
MT2 Fishing Bot - Multi-Window Support
Automated fishing minigame bot for Metin2
Author: boristei

Main entry point - imports all modules and starts the GUI
"""

import ctypes
import asyncio
import sys

# Fix for high DPI displays (125%, 150%, etc.) where UI elements may be cut off
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
except Exception:
    pass  # Older Windows versions may not support this

import os
import threading
import time
import re
import unicodedata
import random
import winsound
from collections import deque
from typing import Optional, Tuple, Dict

import cv2
import numpy as np
import pyautogui

# Disable PyAutoGUI fail-safe for multi-window automation
# When multiple bots run simultaneously, mouse movements can trigger the fail-safe
# This is safe because we have explicit click logic and input_lock synchronization
pyautogui.FAILSAFE = False

from mss import mss

try:
    from winrt.windows.storage import StorageFile
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.globalization import Language
except ImportError:
    StorageFile = None
    BitmapDecoder = None
    OcrEngine = None
    Language = None

try:
    from pynput import keyboard
    from pynput.keyboard import Controller, Key
except ImportError:
    from utils import DEBUG_PRINTS
    if DEBUG_PRINTS:
        print("ERROR: pynput not installed! Install with: pip install pynput")
    keyboard = None
    Controller = None
    Key = None

from utils import get_resource_path, input_lock, play_rickroll_beep, DEBUG_PRINTS, send_scan_key, get_last_win_error
from window_manager import WindowManager, GameRegion
from fish_detector import FishDetector


class FishingBot:
    """Main bot that plays the fishing minigame - one instance per game window"""
    
    # Class-level template cache (shared by all bot instances - loaded only once)
    _template_cache = None
    _template_border_crop = 7  # Pixels to crop from each edge of templates
    _classic_fish_template = None  # Cache for classic fish detection template
    
    # Color templates for fish that look identical in grayscale
    _confusable_fish = {
        'Goldfish_living.jpg',
        'Large_zander_living.jpg',
        'Red_Dye_item.jpg',
        'White_Dye_item.jpg',
        'Yellow_Dye_item.jpg',
        'Brown_Dye_item.jpg',
        'Black_Dye_item.jpg',
        'Bleach_item.jpg',
    }
    _color_template_cache = None  # Cache for colored versions of confusable fish
    _empty_slot_template = None   # Cache for empty_slot.jpg template
    _projekt_hard_bubble_templates = None
    _projekt_hard_number_templates = None
    _projekt_hard_knn_model = None
    _projekt_hard_permit_template = None
    
    def __init__(self, region: GameRegion, config: dict, window_manager: WindowManager, 
                 bait_counter: int = 800, bait_keys: list = None, bot_id: int = 0):
        # Core components
        self.region = region
        self.config = config
        self.window_manager = window_manager
        self.detector = FishDetector()
        self.sct = None  # Screen capture (created per-thread)
        
        # State tracking
        self.running = False
        self.paused = False
        self.hits = 0
        self.total_games = 0
        self.bait_counter = bait_counter
        self.bait_keys = bait_keys if bait_keys else ['1', '2', '3', '4']
        self.region_auto_calibrated = False
        self.consecutive_failures = 0
        self.bot_id = bot_id
        
        # Cached circle values for performance
        self._circle_center = None
        self._circle_radius_sq = 67 * 67
        
        # Lock fairness: prevent one thread from hogging the lock
        self._consecutive_lock_acquisitions = 0
        self._lock_acquisition_limit = 3  # Max consecutive acquisitions before yielding
        self._last_ph_score_log = 0.0
        self._last_ph_debug_save = 0.0
        self._last_ph_sample_save = 0.0
        self._last_ph_sample_log = 0.0
        self._ph_sample_count = 0
        self._ph_sample_active = False
        self._ph_detection_sample_count = 0
        self._ph_detect_log_active = False
        self._last_ph_scores = {}
        self._last_ph_color_confidence = 0.0
        self._last_ph_visible_confidence = 0.0
        self._last_ph_visible_source = "none"
        self._last_ph_best_count = 0
        self._last_ph_best_confidence = 0.0
        self._ph_ocr_engine = None
        self._ph_last_chat_text = ""
        self._ph_last_chat_result = ""
        self._ph_chat_open_attempted = False
        self._ph_seen_private_lines = set()
        self._ph_private_pause_triggered = False
        self._ph_last_bubble_decision_at = 0.0
        self._last_ph_wait_log = 0.0
        self._ph_wait_abort_state = ""
        self._ph_recent_frames = deque(maxlen=28)
        
        # Callbacks for GUI updates
        self.on_status_update = None
        self.on_stats_update = None
        self.on_bait_update = None  # Callback for bait counter changes
        self.on_bot_stop = None  # Callback when bot stops
        
        # Setup keyboard controller (shared, but access controlled by lock)
        self.keyboard_controller = None
        if keyboard and Controller:
            self.keyboard_controller = Controller()
        
        # Inventory capture width (right side of window where items appear)
        self._inventory_width = 200
        
        # Inventory capture Y offset (skip top 300px of window)
        self._inventory_y_offset = 200
        
        # Dead fish tracking: ignored slot positions (10 pixel radius around center)
        self._ignored_positions = set()  # Positions confirmed as dead fish

        # Empty slot tracking for inventory management
        self._empty_slot_positions = []  # Ordered (inv_x, inv_y) of empty slots on current page
        self._current_inv_page = 0       # Current inventory page index (0-based)

        # Timing cache — read from config once per session in play_game(), never inside loops
        self._t_cursor   = config.get('timing_cursor_settle',  0.012)
        self._t_hold     = config.get('timing_button_hold',    0.008)
        self._t_post     = config.get('timing_post_click',     0.035)
        self._t_human_mn = config.get('timing_human_min',      0.15)
        self._t_human_mx = config.get('timing_human_max',      0.40)
        self._t_key_hold = config.get('timing_key_hold',       0.025)
        self._t_key_set  = config.get('timing_key_settle',     0.030)
        self._t_interkey = config.get('timing_cast_interkey',  0.350)
        # Game-response waits (tunable via Timing Settings window)
        self._t_catch_wait  = config.get('timing_catch_wait',        0.400)
        self._t_open_wait   = config.get('timing_open_wait',         0.100)
        self._t_dead_check  = config.get('timing_dead_fish_check',   0.100)
        self._t_drop_settle = config.get('timing_drop_settle',       0.120)
        self._t_qs_between  = config.get('timing_quickskip_between', 0.100)
        self._t_qs_after    = config.get('timing_quickskip_after',   0.100)
        self._t_ph_bubble_timeout = max(30.000, config.get('timing_projekt_bubble_timeout', 35.000))
        self._t_ph_retry_wait     = config.get('timing_projekt_retry_wait',     0.700)
        self._t_ph_space_gap      = config.get('timing_projekt_space_gap',      0.300)
        self._t_ph_bait_to_cast_min = config.get('timing_projekt_bait_to_cast_min', 0.152)
        self._t_ph_bait_to_cast_max = config.get('timing_projekt_bait_to_cast_max', 0.746)
        self._t_ph_space_gap_min = config.get('timing_projekt_space_gap_min', 0.300)
        self._t_ph_space_gap_max = config.get('timing_projekt_space_gap_max', 0.800)
        self._t_ph_after_result_min = config.get('timing_projekt_after_result_min', 1.000)
        self._t_ph_after_result_max = config.get('timing_projekt_after_result_max', 5.000)
        self._t_ph_post_reel_wait = config.get('timing_projekt_post_reel_wait', 5.000)
        self._t_ph_pre_reel_wait  = config.get('timing_projekt_pre_reel_wait',  1.500)
        self._t_ph_space_hold     = config.get('timing_projekt_space_hold',     0.080)

    def _random_timing_delay(self, min_value: float, max_value: float) -> float:
        """Returns a random delay, tolerating stale configs with swapped bounds."""
        min_value = max(0.0, float(min_value))
        max_value = max(0.0, float(max_value))
        if max_value < min_value:
            min_value, max_value = max_value, min_value
        return random.uniform(min_value, max_value)
        
    def _load_template_cache(self) -> Dict[str, tuple]:
        """Loads all fish/item templates from assets folder into class-level cache.
        Returns dict of {filename: (grayscale_template, half_width, half_height)}
        Templates are cropped by 7 pixels on each edge to focus on center.
        Cache is shared by all bot instances - loaded only once globally.
        Pre-computes half dimensions for faster center calculation."""
        # Check class-level cache first (shared by all instances)
        if FishingBot._template_cache is not None:
            return FishingBot._template_cache
        
        FishingBot._template_cache = {}
        assets_path = get_resource_path("assets")
        
        if not os.path.exists(assets_path):
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Assets folder not found!")
            return FishingBot._template_cache
        
        border = FishingBot._template_border_crop
        
        for f in os.listdir(assets_path):
            if f.endswith('_living.jpg') or f.endswith('_living.png') or \
               f.endswith('_item.jpg') or f.endswith('_item.png'):
                try:
                    img_path = os.path.join(assets_path, f)
                    template = cv2.imread(img_path)
                    if template is not None:
                        template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)

                        # Crop border from all edges (focus on center)
                        h, w = template_gray.shape
                        if h > border * 2 and w > border * 2:
                            template_gray = template_gray[border:h-border, border:w-border]
                        
                        # Pre-compute half dimensions for center calculation
                        h, w = template_gray.shape
                        FishingBot._template_cache[f] = (template_gray, w >> 1, h >> 1)
                except Exception as e:
                    if self.on_status_update:
                        self.on_status_update(f"[W{self.bot_id+1}] Error loading template {f}: {e}")
        
        if self.on_status_update:
            self.on_status_update(f"[W{self.bot_id+1}] Loaded {len(FishingBot._template_cache)} item templates (grayscale, cropped {border}px)")
        return FishingBot._template_cache
    
    def _load_color_template_cache(self) -> Dict[str, tuple]:
        """Loads color versions of confusable fish templates for disambiguation.
        Returns dict of {filename: (bgr_template, half_width, half_height)}"""
        if FishingBot._color_template_cache is not None:
            return FishingBot._color_template_cache
        
        FishingBot._color_template_cache = {}
        assets_path = get_resource_path("assets")
        
        if not os.path.exists(assets_path):
            return FishingBot._color_template_cache
        
        border = FishingBot._template_border_crop
        
        for filename in FishingBot._confusable_fish:
            try:
                img_path = os.path.join(assets_path, filename)
                if os.path.exists(img_path):
                    template = cv2.imread(img_path)
                    if template is not None:
                        h, w = template.shape[:2]
                        if h > border * 2 and w > border * 2:
                            template = template[border:h-border, border:w-border]
                        
                        h, w = template.shape[:2]
                        FishingBot._color_template_cache[filename] = (template, w >> 1, h >> 1)
            except Exception:
                continue
        
        if self.on_status_update and FishingBot._color_template_cache:
            self.on_status_update(f"[W{self.bot_id+1}] Loaded {len(FishingBot._color_template_cache)} color templates for disambiguation")

        return FishingBot._color_template_cache

    def _load_empty_slot_template(self):
        """Loads assets/empty_slot.jpg for empty inventory slot detection."""
        if FishingBot._empty_slot_template is not None:
            return FishingBot._empty_slot_template

        assets_path = get_resource_path("assets")
        img_path = os.path.join(assets_path, "empty_slot.jpg")

        if not os.path.exists(img_path):
            return None

        try:
            template = cv2.imread(img_path)
            if template is None:
                return None

            template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
            border = FishingBot._template_border_crop
            h, w = template_gray.shape
            if h > border * 2 and w > border * 2:
                template_gray = template_gray[border:h-border, border:w-border]

            h, w = template_gray.shape
            FishingBot._empty_slot_template = (template_gray, w >> 1, h >> 1)
        except Exception as e:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Error loading empty slot template: {e}")

        return FishingBot._empty_slot_template

    def _scan_empty_slots(self, inventory_frame: np.ndarray) -> list:
        """Finds all empty inventory slot positions in the frame using template matching.
        Returns an ordered list of (inv_x, inv_y) sorted top-to-bottom, left-to-right.

        Optimized: single matchTemplate + vectorized np.where + sort-based NMS,
        replacing the previous iterative minMaxLoc+mask loop that re-scanned the
        result array once per detected slot (~25 full passes)."""
        slot_template = self._load_empty_slot_template()
        if slot_template is None:
            return []

        template, half_w, half_h = slot_template
        t_h, t_w = template.shape

        inventory_gray = cv2.cvtColor(inventory_frame, cv2.COLOR_BGR2GRAY)
        inv_h, inv_w = inventory_gray.shape

        if t_h > inv_h or t_w > inv_w:
            return []

        THRESHOLD = 0.70
        try:
            result = cv2.matchTemplate(inventory_gray, template, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(result >= THRESHOLD)
            if ys.size == 0:
                return []

            # Sort all candidates by score descending, then keep only those
            # outside a 10px radius of any already-kept point (cheap NMS).
            scores = result[ys, xs]
            order = np.argsort(scores)[::-1]

            kept = []
            for idx in order:
                cx = int(xs[idx]) + half_w
                cy = int(ys[idx]) + half_h
                dup = False
                for ex, ey in kept:
                    if abs(cx - ex) < 10 and abs(cy - ey) < 10:
                        dup = True
                        break
                if not dup:
                    kept.append((cx, cy))

            kept.sort(key=lambda p: (p[1] // 30, p[0]))
            return kept

        except Exception:
            return []

    def _disambiguate_confusable_fish(self, inventory_frame_color: np.ndarray, inv_x: int, inv_y: int, matched_filename: str) -> str:
        """Disambiguates between fish that look identical in grayscale using color comparison.
        Returns the correct filename after color-based verification.
        
        Args:
            inventory_frame_color: BGR color inventory frame
            inv_x, inv_y: Center position of the detected fish in inventory
            matched_filename: The filename that was matched in grayscale
        
        Returns:
            Correct filename after color verification
        """
        color_templates = self._load_color_template_cache()
        if not color_templates:
            return matched_filename  # Fallback to original match
        
        # Get dimensions from matched template to extract region
        gray_templates = self._load_template_cache()
        if matched_filename not in gray_templates:
            return matched_filename
        
        _, half_w, half_h = gray_templates[matched_filename]
        
        # Extract region around the detected fish (use template size)
        inv_h, inv_w = inventory_frame_color.shape[:2]
        x1 = max(0, inv_x - half_w - 5)
        y1 = max(0, inv_y - half_h - 5)
        x2 = min(inv_w, inv_x + half_w + 5)
        y2 = min(inv_h, inv_y + half_h + 5)
        
        region = inventory_frame_color[y1:y2, x1:x2]
        if region.size == 0:
            return matched_filename
        
        best_match = matched_filename
        best_confidence = 0.0
        
        # Compare against all confusable fish color templates
        for filename in FishingBot._confusable_fish:
            if filename not in color_templates:
                continue
            
            color_template, _, _ = color_templates[filename]
            t_h, t_w = color_template.shape[:2]
            r_h, r_w = region.shape[:2]
            
            # Skip if template is larger than region
            if t_h > r_h or t_w > r_w:
                continue
            
            try:
                # Color template matching (BGR)
                result = cv2.matchTemplate(region, color_template, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(result)
                
                if max_val > best_confidence:
                    best_confidence = max_val
                    best_match = filename
            except Exception:
                continue
        
        if best_match != matched_filename and self.on_status_update:
            self.on_status_update(f"[W{self.bot_id+1}] Color disambiguation: {matched_filename} -> {best_match} (conf: {best_confidence:.2f})")
        
        return best_match
    
    def capture_inventory_area(self) -> np.ndarray:
        """Captures the inventory area (right 270px of the game window, starting at y=300)."""
        try:
            if self.sct is None:
                self.sct = mss()
            
            win_left, win_top, win_width, win_height = self.window_manager.get_window_rect()
            
            # Capture right 270px of window, starting from y=300 (skip top 300px and bottom 30px)
            monitor = {
                "left": win_left + win_width - self._inventory_width,
                "top": win_top + self._inventory_y_offset,
                "width": self._inventory_width,
                "height": max(0, win_height - self._inventory_y_offset - 30)
            }
            
            sct_img = self.sct.grab(monitor)
            return np.ascontiguousarray(np.asarray(sct_img, dtype=np.uint8)[:, :, :3])
        except Exception as e:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Error capturing inventory: {e}")
            return np.zeros((100, 100, 3), dtype=np.uint8)
    
    def identify_item_in_inventory(self, inventory_frame: np.ndarray, ignore_positions: set = None) -> Optional[Tuple[str, Tuple[int, int]]]:
        """Identifies an item in the inventory using template matching with high precision.
        Returns (filename, (x, y)) of best match or None if no match found.
        Coordinates are relative to inventory area.
        ignore_positions: set of (x, y) tuples to skip (dead fish locations).
        If first match is ignored, tries to find another match within same template.
        
        For confusable fish (Goldfish vs Large_zander), uses color-based disambiguation."""
        templates = self._load_template_cache()
        if not templates:
            return None
        
        # Convert inventory to grayscale once (keep color frame for disambiguation)
        inventory_gray = cv2.cvtColor(inventory_frame, cv2.COLOR_BGR2GRAY)
        inv_h, inv_w = inventory_gray.shape
        
        # Local references for speed
        match_template = cv2.matchTemplate
        minMaxLoc = cv2.minMaxLoc
        TM_CCOEFF_NORMED = cv2.TM_CCOEFF_NORMED
        CONFIDENCE_THRESHOLD = 0.80  # Lowered from 0.8 for better detection
        EARLY_EXIT_THRESHOLD = 0.90  # Near-perfect match, skip remaining templates
        confusable_fish = FishingBot._confusable_fish
        
        best_match = None
        best_confidence = CONFIDENCE_THRESHOLD  # Start at threshold (only accept better)
        
        for filename, (template, half_w, half_h) in templates.items():
            t_h, t_w = template.shape

            # Skip if template larger than inventory
            if t_h > inv_h or t_w > inv_w:
                continue

            try:
                result = match_template(inventory_gray, template, TM_CCOEFF_NORMED)
                result_copy = result.copy()

                # Try to find first non-ignored match for this template
                while True:
                    _, max_val, _, max_loc = minMaxLoc(result_copy)

                    # Stop if no more good matches
                    if max_val <= 0.5:
                        break

                    pt_x, pt_y = max_loc
                    center_x = pt_x + half_w
                    center_y = pt_y + half_h

                    # Check if this match is in ignore list
                    is_ignored = False
                    if ignore_positions:
                        for ix, iy in ignore_positions:
                            if abs(center_x - ix) < 10 and abs(center_y - iy) < 10:
                                is_ignored = True
                                break

                    # If not ignored and better than current best, accept it
                    if not is_ignored and max_val > best_confidence:
                        best_confidence = max_val
                        matched_filename = filename

                        # Disambiguate confusable fish using color comparison
                        if filename in confusable_fish:
                            matched_filename = self._disambiguate_confusable_fish(
                                inventory_frame, center_x, center_y, filename
                            )

                        best_match = (matched_filename, (center_x, center_y))

                        # Early exit on near-perfect match (but NOT for confusable fish)
                        if best_confidence >= EARLY_EXIT_THRESHOLD and filename not in confusable_fish:
                            return best_match
                        break  # Found good match for this template, move to next template

                    # Mask out this match to try next one within same template
                    mask_x1 = max(0, pt_x - t_w // 2)
                    mask_y1 = max(0, pt_y - t_h // 2)
                    mask_x2 = min(result_copy.shape[1], pt_x + t_w // 2 + 1)
                    mask_y2 = min(result_copy.shape[0], pt_y + t_h // 2 + 1)
                    result_copy[mask_y1:mask_y2, mask_x1:mask_x2] = -1.0

            except Exception:
                continue

        return best_match
    
    def _is_item_at_position(self, inventory_frame: np.ndarray, x: int, y: int, radius: int = 10) -> bool:
        """Checks if a slot at (x, y) is still occupied by an item.
        Optimized: instead of running matchTemplate against every fish/item template
        (O(N) heavy convolutions), we check whether the empty_slot template fits at
        this position. If it matches strongly, the slot is empty -> item is gone.
        Single matchTemplate call on a tiny crop instead of N full-frame searches."""
        slot_template = self._load_empty_slot_template()
        if slot_template is None:
            # Fallback to old path if empty_slot template missing
            return self._is_item_at_position_fallback(inventory_frame, x, y, radius)

        template, half_w, half_h = slot_template
        t_h, t_w = template.shape

        # Crop a small search window around the target position. The match window
        # only needs to be slightly bigger than the template + radius.
        pad = radius + 4
        x1 = max(0, x - half_w - pad)
        y1 = max(0, y - half_h - pad)
        x2 = min(inventory_frame.shape[1], x + half_w + pad)
        y2 = min(inventory_frame.shape[0], y + half_h + pad)

        crop = inventory_frame[y1:y2, x1:x2]
        if crop.shape[0] < t_h or crop.shape[1] < t_w:
            return self._is_item_at_position_fallback(inventory_frame, x, y, radius)

        try:
            crop_gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            result = cv2.matchTemplate(crop_gray, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            # High empty-slot confidence => slot is empty => no item present
            return max_val < 0.70
        except Exception:
            return self._is_item_at_position_fallback(inventory_frame, x, y, radius)

    def _is_item_at_position_fallback(self, inventory_frame: np.ndarray, x: int, y: int, radius: int = 10) -> bool:
        """Fallback heavy check: matches every item template (used only if empty_slot
        template is unavailable or the crop is too small)."""
        templates = self._load_template_cache()
        if not templates:
            return False

        inventory_gray = cv2.cvtColor(inventory_frame, cv2.COLOR_BGR2GRAY)
        inv_h, inv_w = inventory_gray.shape

        match_template = cv2.matchTemplate
        where = np.where
        TM_CCOEFF_NORMED = cv2.TM_CCOEFF_NORMED

        for template, half_w, half_h in templates.values():
            t_h, t_w = template.shape
            if t_h > inv_h or t_w > inv_w:
                continue
            try:
                result = match_template(inventory_gray, template, TM_CCOEFF_NORMED)
                locations = where(result >= 0.8)
                if locations[0].size == 0:
                    continue
                for pt_y, pt_x in zip(locations[0], locations[1]):
                    if abs((pt_x + half_w) - x) < radius and abs((pt_y + half_h) - y) < radius:
                        return True
            except Exception:
                continue
        return False
    
    def handle_caught_item(self):
        """Identifies and handles caught item based on fish_actions config.
        Should be called after a successful catch.
        After clicking, immediately checks if fish is still there - if so, adds to ignore list.
        
        IMPORTANT: The entire detection + click sequence must be atomic to prevent
        another bot from interfering between detection and action."""
        if not self.config.get('auto_fish_handling', False):
            if self.config.get('projekt_hard_fishing', False):
                time.sleep(self._t_ph_post_reel_wait)
                return
            # Even without auto handling, track inventory fullness so page-switching works
            try:
                time.sleep(self._t_catch_wait)
                inventory_frame = self.capture_inventory_area()
                self._empty_slot_positions = self._scan_empty_slots(inventory_frame)
                if not self._empty_slot_positions:
                    self._switch_inventory_page()
            except Exception:
                pass
            return

        fish_actions = self.config.get('fish_actions', {})
        if not fish_actions:
            return

        try:
            # Activate window and wait for item to land in inventory (outside lock — no input needed)
            with input_lock:
                self.window_manager.activate_window(force_activate=True)
            time.sleep(self._t_catch_wait)

            # Identify item (outside lock — read-only screen capture)
            inventory_frame = self.capture_inventory_area()
            match = self.identify_item_in_inventory(inventory_frame, ignore_positions=self._ignored_positions)
            if not match:
                return

            filename, (inv_x, inv_y) = match
            action = fish_actions.get(filename, 'keep')

            fish_name = filename.replace('_living.jpg', '').replace('_item.jpg', '')

            # ========== SINGLE LOCK COVERS THE ENTIRE INTERACTION ==========
            # Holding the lock from first click to last click prevents another
            # bot from moving the mouse between our steps.  Screen captures
            # (mss.grab) are safe inside the lock — they don't send input.
            with input_lock:
                self.window_manager.activate_window(force_activate=True)
                win_left, win_top, win_width, win_height = self.window_manager.get_window_rect()
                screen_x = win_left + win_width - self._inventory_width + inv_x
                screen_y = win_top + self._inventory_y_offset + inv_y
                win_center_x = win_left + win_width // 2
                win_center_y = win_top + win_height // 2

                if action == 'keep':
                    self._ignored_positions.add((inv_x, inv_y))
                    self._empty_slot_positions = [
                        (ex, ey) for ex, ey in self._empty_slot_positions
                        if not (abs(inv_x - ex) < 15 and abs(inv_y - ey) < 15)
                    ]
                    if self.on_status_update:
                        self.on_status_update(
                            f"[W{self.bot_id+1}] Keeping: {fish_name} "
                            f"(ignored, {len(self._empty_slot_positions)} empty slots left)")

                elif action == 'open':
                    if self.on_status_update:
                        self.on_status_update(f"[W{self.bot_id+1}] Opening: {fish_name}")

                    pyautogui.moveTo(screen_x, screen_y, _pause=False)
                    time.sleep(0.05)
                    pyautogui.click(button='right', _pause=False)
                    time.sleep(self._t_open_wait)
                    pyautogui.moveTo(win_center_x, win_center_y, _pause=False)

                    # Verify while still holding the lock (no other bot can interfere)
                    time.sleep(self._t_open_wait)
                    inv_check = self.capture_inventory_area()
                    still_there = self._is_item_at_position(inv_check, inv_x, inv_y)

                    if still_there:
                        time.sleep(self._t_dead_check)
                        inv_check2 = self.capture_inventory_area()
                        still_there = self._is_item_at_position(inv_check2, inv_x, inv_y)

                    if still_there:
                        # Dead fish — permanently occupies this slot
                        self._ignored_positions.add((inv_x, inv_y))
                        self._empty_slot_positions = [
                            (ex, ey) for ex, ey in self._empty_slot_positions
                            if not (abs(inv_x - ex) < 15 and abs(inv_y - ey) < 15)
                        ]
                    else:
                        # Fish was opened — slot is now free
                        if not any(abs(inv_x - ex) < 15 and abs(inv_y - ey) < 15
                                   for ex, ey in self._empty_slot_positions):
                            self._empty_slot_positions.append((inv_x, inv_y))
                            self._empty_slot_positions.sort(key=lambda p: (p[1] // 30, p[0]))

                elif action == 'drop':
                    confirm_pos = self.config.get('confirm_button_pos')
                    drop_pos    = self.config.get('drop_button_pos')

                    if not confirm_pos:
                        if self.on_status_update:
                            self.on_status_update(f"[W{self.bot_id+1}] Confirm button not configured! Keeping: {fish_name}")
                        self._ignored_positions.add((inv_x, inv_y))
                        self._empty_slot_positions = [
                            (ex, ey) for ex, ey in self._empty_slot_positions
                            if not (abs(inv_x - ex) < 15 and abs(inv_y - ey) < 15)
                        ]
                        return  # exit inside lock — lock released by context manager

                    if self.on_status_update:
                        self.on_status_update(f"[W{self.bot_id+1}] Dropping: {fish_name}")

                    is_fish = '_living' in filename
                    still_there = True

                    if is_fish:
                        # Right-click to test if fish can be opened
                        pyautogui.moveTo(screen_x, screen_y, _pause=False)
                        time.sleep(0.05)
                        pyautogui.click(button='right', _pause=False)
                        time.sleep(self._t_open_wait)
                        pyautogui.moveTo(win_center_x, win_center_y, _pause=False)

                        # Check result while still holding the lock
                        time.sleep(self._t_open_wait)
                        inv_check = self.capture_inventory_area()
                        still_there = self._is_item_at_position(inv_check, inv_x, inv_y)

                    if still_there:
                        # ========== DROP SEQUENCE (all inside lock) ==========
                        pyautogui.moveTo(screen_x, screen_y, _pause=False)
                        time.sleep(0.05)
                        pyautogui.click(_pause=False)
                        time.sleep(self._t_drop_settle)

                        pyautogui.moveTo(win_center_x, win_center_y, _pause=False)
                        time.sleep(0.05)
                        pyautogui.click(_pause=False)
                        time.sleep(self._t_drop_settle)

                        if drop_pos:
                            pyautogui.moveTo(win_left + drop_pos[0], win_top + drop_pos[1], _pause=False)
                            time.sleep(0.05)
                            pyautogui.click(_pause=False)
                            time.sleep(self._t_drop_settle)

                        pyautogui.moveTo(win_left + confirm_pos[0], win_top + confirm_pos[1], _pause=False)
                        time.sleep(0.05)
                        pyautogui.click(_pause=False)
                        time.sleep(self._t_drop_settle)

                        pyautogui.moveTo(win_center_x, win_center_y, _pause=False)
            # ========== LOCK RELEASED ==========

            # Page-switch if needed — acquires its own lock internally
            if action == 'keep' and not self._empty_slot_positions:
                self._switch_inventory_page()
            elif action == 'open' and not self._empty_slot_positions:
                self._switch_inventory_page()

        except Exception:
            pass
        
    def _update_region_cache(self):
        """Updates cached constants when region changes."""
        if self.region:
            self._circle_center = (self.region.width >> 1, self.region.height >> 1)  # Bitwise divide by 2
        else:
            self._circle_center = None
    
    def capture_full_window(self) -> np.ndarray:
        """Captures the entire game window for initial detection."""
        try:
            if self.sct is None:
                self.sct = mss()
            
            win_left, win_top, win_width, win_height = self.window_manager.get_window_rect()
            
            monitor = {
                "left": win_left,
                "top": win_top,
                "width": win_width,
                "height": win_height
            }
            
            sct_img = self.sct.grab(monitor)
            return np.ascontiguousarray(np.asarray(sct_img, dtype=np.uint8)[:, :, :3])
        except Exception as e:
            if self.on_status_update:
                self.on_status_update(f"Screenshot error: {e}")
            return np.zeros((100, 100, 3), dtype=np.uint8)
    
    def capture_screen(self) -> np.ndarray:
        """Captures the game region as a numpy array for processing."""
        try:
            if self.sct is None:
                self.sct = mss()

            if not self.region:
                return self.capture_full_window()

            win_left, win_top, _, _ = self.window_manager.get_window_rect()
            screen_left = win_left + self.region.left
            screen_top = win_top + self.region.top

            monitor = {
                "left": screen_left,
                "top": screen_top,
                "width": self.region.width,
                "height": self.region.height
            }

            sct_img = self.sct.grab(monitor)
            # Direct slice from BGRA buffer to contiguous BGR view — avoids the full
            # cvtColor pass over every pixel (significant on hot-loop captures).
            return np.ascontiguousarray(np.asarray(sct_img, dtype=np.uint8)[:, :, :3])
        except Exception as e:
            if self.on_status_update:
                self.on_status_update(f"Screenshot error: {e}")
            if self.region:
                return np.zeros((self.region.height, self.region.width, 3), dtype=np.uint8)
            return np.zeros((100, 100, 3), dtype=np.uint8)
    
    def atomic_capture_and_click(self) -> Tuple[bool, Optional[Tuple[int, int]]]:
        """Captures screen and clicks fish if in circle. Optimized single-pass detection.
        Returns: (minigame_active, fish_position_clicked or None)"""
        # Local references for speed
        capture = self.capture_screen
        detect = self.detector.detect_window_and_fish
        circle_center = self._circle_center
        radius_sq = self._circle_radius_sq
        region_left = self.region.left
        region_top = self.region.top
        
        try:
            # ========== PHASE 1: Quick pre-check (NO LOCK) ==========
            frame = capture()
            window_active, fish_pos = detect(frame)
            
            if not window_active:
                return (False, None)
            if not fish_pos:
                return (True, None)
            
            # Inline circle check for speed
            fx, fy = fish_pos
            cx, cy = circle_center
            dx, dy = fx - cx, fy - cy
            if (dx * dx + dy * dy) >= radius_sq:
                # Fish not in circle - reset consecutive lock counter
                self._consecutive_lock_acquisitions = 0
                return (True, None)
            
            # Fish is in circle! Now get lock and click
            # ========== PHASE 2: Fresh capture + click (WITH LOCK) ==========
            with input_lock:
                # Activate window
                self.window_manager.activate_window(force_activate=True)
                
                # RE-CAPTURE fresh frame
                frame = capture()
                window_active, fish_pos = detect(frame)
                
                if not window_active:
                    self._consecutive_lock_acquisitions = 0
                    return (False, None)
                if not fish_pos:
                    self._consecutive_lock_acquisitions = 0
                    return (True, None)
                
                # Inline circle check
                fx, fy = fish_pos
                dx, dy = fx - cx, fy - cy
                if (dx * dx + dy * dy) >= radius_sq:
                    self._consecutive_lock_acquisitions = 0
                    return (True, None)
                
                # Click at FRESH position
                win_left, win_top, _, _ = self.window_manager.get_window_rect()
                screen_x = win_left + region_left + fx
                screen_y = win_top + region_top + fy
                
                # Optimized click sequence (uses pre-cached timing vars, no config reads here)
                pyautogui.moveTo(screen_x, screen_y, _pause=False)
                time.sleep(self._t_cursor)
                pyautogui.mouseDown(_pause=False)
                time.sleep(self._t_hold)
                pyautogui.mouseUp(_pause=False)
                # mouseUp is already sent to the OS — release lock immediately
                self._consecutive_lock_acquisitions += 1
            # ========== LOCK RELEASED ==========

            # Post-click settle and fairness yield both happen outside lock so
            # other threads can acquire it without waiting for our sleeps
            time.sleep(self._t_post)

            # Fairness: yield to other threads if this thread has been acquiring lock too often
            if self._consecutive_lock_acquisitions >= self._lock_acquisition_limit:
                self._consecutive_lock_acquisitions = 0
                time.sleep(0.05)  # 50ms yield to allow other threads to compete for lock
            
            return (True, fish_pos)
            
        except Exception as e:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Click error: {e}")
            return (True, None)
    
    def get_bait_key(self, bait_count: int) -> str:
        """Determines which keyboard key to press based on bait counter and selected keys."""
        if not self.bait_keys:
            return '1'
        
        num_keys = len(self.bait_keys)
        bait_per_key = 200
        
        # Calculate which key index to use based on bait count
        # Keys are used from first to last as bait depletes
        for i, key in enumerate(self.bait_keys):
            threshold = (num_keys - i - 1) * bait_per_key
            if bait_count > threshold:
                return key
        
        # If bait count is very low, use the last key
        return self.bait_keys[-1]
    
    def get_tier_thresholds(self) -> list:
        """Returns list of tier thresholds based on selected keys."""
        num_keys = len(self.bait_keys)
        # Create thresholds: e.g., for 4 keys: [600, 400, 200, 0]
        return [(num_keys - i - 1) * 200 for i in range(num_keys)]
    
    def adjust_bait_tier(self):
        """Adjusts bait counter to next lower tier when 2 consecutive failures occur."""
        thresholds = self.get_tier_thresholds()
        
        # Find current tier and drop to next one
        for threshold in thresholds:
            if self.bait_counter > threshold:
                self.bait_counter = threshold
                break
        else:
            # Already at or below lowest threshold
            self.bait_counter = 0
        
        self.consecutive_failures = 0
        
        if self.on_status_update:
            self.on_status_update(f"[W{self.bot_id+1}] 2 consecutive failures! Bait adjusted to {self.bait_counter}")
        if self.on_bait_update:
            self.on_bait_update(self.bot_id, self.bait_counter)
        if self.on_stats_update:
            self.on_stats_update(self.bot_id, self.hits, self.total_games, self.bait_counter)
    
    def press_ctrl_key(self, key: str):
        """Presses CTRL+key combination once. Uses input lock for thread safety."""
        if not self.keyboard_controller:
            return
        
        with input_lock:
            try:
                self.window_manager.activate_window()
                time.sleep(self._t_key_set)
                self.keyboard_controller.press(Key.ctrl)
                time.sleep(self._t_key_hold)
                self.keyboard_controller.press(key)
                time.sleep(self._t_key_hold)
                self.keyboard_controller.release(key)
                time.sleep(self._t_key_hold)
                self.keyboard_controller.release(Key.ctrl)
            except Exception as e:
                if self.on_status_update:
                    self.on_status_update(f"[W{self.bot_id+1}] Error pressing CTRL+{key}: {e}")
    
    def bait_and_cast(self):
        """Selects bait and casts fishing line in a single lock acquisition."""
        if not self.keyboard_controller:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Keyboard controller not available; cannot send bait/cast keys")
            return

        bait_key = self.get_bait_key(self.bait_counter)
        key_map = {'space': Key.space, 'F1': Key.f1, 'F2': Key.f2, 'F3': Key.f3, 'F4': Key.f4}

        with input_lock:
            try:
                self.window_manager.activate_window()
                time.sleep(self._t_key_set)

                # Some game clients ignore synthetic keys unless the game window
                # has real foreground focus. Click the title area, not the viewport,
                # so the cursor does not cover or disturb the fish bubble area.
                try:
                    win_left, win_top, win_width, win_height = self.window_manager.get_window_rect()
                    focus_x = win_left + max(80, min(win_width - 80, win_width // 2))
                    focus_y = win_top + 12
                    pyautogui.click(focus_x, focus_y, _pause=False)
                    time.sleep(self._t_key_set)
                except Exception as focus_error:
                    if self.on_status_update:
                        self.on_status_update(f"[W{self.bot_id+1}] Focus click failed: {focus_error}")

                pynput_bait = key_map.get(bait_key.upper() if len(bait_key) > 1 else bait_key, key_map.get(bait_key, bait_key))
                if not send_scan_key(bait_key, self._t_key_hold):
                    if self.on_status_update:
                        self.on_status_update(f"[W{self.bot_id+1}] SendInput failed for {bait_key} (winerr {get_last_win_error()}); falling back to pynput")
                    self.keyboard_controller.press(pynput_bait)
                    time.sleep(self._t_key_hold)
                    self.keyboard_controller.release(pynput_bait)
                if self.on_status_update:
                    self.on_status_update(f"[W{self.bot_id+1}] Pressed key {bait_key}")

                time.sleep(self._t_interkey)

                if not send_scan_key('space', self._t_key_hold):
                    if self.on_status_update:
                        self.on_status_update(f"[W{self.bot_id+1}] SendInput failed for space (winerr {get_last_win_error()}); falling back to pynput")
                    self.keyboard_controller.press(Key.space)
                    time.sleep(self._t_key_hold)
                    self.keyboard_controller.release(Key.space)
                if self.on_status_update:
                    self.on_status_update(f"[W{self.bot_id+1}] Cast fishing line")
            except Exception as e:
                if self.on_status_update:
                    self.on_status_update(f"[W{self.bot_id+1}] Error in bait_and_cast: {e}")

        time.sleep(0.05)

    def projekt_bait_and_cast(self) -> str:
        """Projekt Hard flow: bait first, optionally confirm in chat, then cast."""
        if not self.keyboard_controller:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Keyboard controller not available; cannot send bait/cast keys")
            return ""

        bait_key = self.get_bait_key(self.bait_counter)
        key_map = {'space': Key.space, 'F1': Key.f1, 'F2': Key.f2, 'F3': Key.f3, 'F4': Key.f4}
        chat_before_bait = ""
        chat_before_cast = ""
        if self._projekt_ocr_available():
            chat_before_bait = self.read_projekt_chat_text()

        bait_confirmed = False
        max_bait_attempts = 3 if self._projekt_ocr_available() else 1
        for bait_attempt in range(1, max_bait_attempts + 1):
            with input_lock:
                try:
                    self.window_manager.activate_window()
                    time.sleep(self._t_key_set)
                    try:
                        win_left, win_top, win_width, _ = self.window_manager.get_window_rect()
                        focus_x = win_left + max(80, min(win_width - 80, win_width // 2))
                        focus_y = win_top + 12
                        pyautogui.click(focus_x, focus_y, _pause=False)
                        time.sleep(self._t_key_set)
                    except Exception:
                        pass

                    pynput_bait = key_map.get(bait_key.upper() if len(bait_key) > 1 else bait_key, key_map.get(bait_key, bait_key))
                    if not send_scan_key(bait_key, self._t_key_hold):
                        self.keyboard_controller.press(pynput_bait)
                        time.sleep(self._t_key_hold)
                        self.keyboard_controller.release(pynput_bait)
                    if self.on_status_update:
                        self.on_status_update(f"[W{self.bot_id+1}] Cebo: tecla {bait_key} enviada ({bait_attempt}/{max_bait_attempts})")
                except Exception as e:
                    if self.on_status_update:
                        self.on_status_update(f"[W{self.bot_id+1}] Error sending PH bait key: {e}")

            if not self._projekt_ocr_available():
                bait_confirmed = True
                break
            if self.wait_for_projekt_bait_selected(timeout=0.8, baseline_text=chat_before_bait):
                bait_confirmed = True
                break
            chat_before_bait = self.read_projekt_chat_text()
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] OCR no confirmo cebo; reintento F4")

        if self._projekt_ocr_available() and not bait_confirmed:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] No confirme cebo por OCR; no lanzo caña")
            bait_confirmed = True

        if self._projekt_ocr_available():
            chat_before_cast = self.read_projekt_chat_text()

        delay = self._random_timing_delay(self._t_ph_bait_to_cast_min, self._t_ph_bait_to_cast_max)
        if self.on_status_update:
            self.on_status_update(f"[W{self.bot_id+1}] Delay cebo->caña: {int(delay * 1000)}ms")
        time.sleep(delay)

        with input_lock:
            try:
                self.window_manager.activate_window()
                time.sleep(self._t_key_set)
                if not send_scan_key('space', self._t_key_hold):
                    self.keyboard_controller.press(Key.space)
                    time.sleep(self._t_key_hold)
                    self.keyboard_controller.release(Key.space)
                if self.on_status_update:
                    self.on_status_update(f"[W{self.bot_id+1}] Caña lanzada")
            except Exception as e:
                if self.on_status_update:
                    self.on_status_update(f"[W{self.bot_id+1}] Error casting PH line: {e}")

        time.sleep(0.05)
        return chat_before_cast
    
    def quickskip(self):
        """Performs quick skip - uses different method based on mode (horse or armour)."""
        # Get quick skip mode from config (default to 'horse' if not set)
        quick_skip_mode = self.config.get('quick_skip_mode', 'horse')
        
        if quick_skip_mode == 'horse':
            # Horse mode: double press CTRL+G
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Quick skip (Horse mode - CTRL+G)...")
            self.press_ctrl_key('g')
            time.sleep(0.1)  # Longer delay for game to process first CTRL+G
            self.press_ctrl_key('g')
            time.sleep(0.1)  # Delay after second press before next action
        else:
            # Armour mode: right-click on armor slot to equip/unequip
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Quick skip (Armor mode - right-click)...")
            
            armor_pos = self.config.get('armor_slot_pos')
            if not armor_pos:
                if self.on_status_update:
                    self.on_status_update(f"[W{self.bot_id+1}] Armor slot position not set! Falling back to wait.")
                time.sleep(0.3)  # Fallback delay
                return
            
            # Acquire lock for mouse operation
            with input_lock:
                # Activate window
                self.window_manager.activate_window(force_activate=True)
                time.sleep(0.03)
                
                # Convert armor slot position (relative to window) to screen coordinates
                win_left, win_top, _, _ = self.window_manager.get_window_rect()
                screen_x = win_left + armor_pos[0]
                screen_y = win_top + armor_pos[1]
                
                # Right-click on armor slot
                pyautogui.moveTo(screen_x, screen_y, _pause=False)
                time.sleep(np.random.uniform(0.2, 0.25))  # cursor settle + human-like jitter
                pyautogui.click(button='right', _pause=False)
                # click() already sent to OS — release lock now
            # ========== LOCK RELEASED ==========
            time.sleep(np.random.uniform(0.05, 0.07))  # animation settle outside lock
            return
    
    def press_key(self, key: str, description: str = ""):
        """Presses a keyboard key using pynput. Uses input lock for thread safety."""
        if not self.keyboard_controller:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Keyboard controller not available; cannot press {key}")
            return
        
        # Map keys to pynput Key objects
        key_map = {
            'space': Key.space, 'F1': Key.f1, 'F2': Key.f2, 'F3': Key.f3, 'F4': Key.f4
        }
        
        with input_lock:
            try:
                self.window_manager.activate_window()
                time.sleep(self._t_key_set)

                pynput_key = key_map.get(key.upper() if len(key) > 1 else key, key_map.get(key, key))

                if not send_scan_key(key, self._t_key_hold):
                    if self.on_status_update:
                        self.on_status_update(f"[W{self.bot_id+1}] SendInput failed for {key} (winerr {get_last_win_error()}); falling back to pynput")
                    self.keyboard_controller.press(pynput_key)
                    time.sleep(self._t_key_hold)
                    self.keyboard_controller.release(pynput_key)

                if description and self.on_status_update:
                    self.on_status_update(f"[W{self.bot_id+1}] {description}")
            except Exception as e:
                if self.on_status_update:
                    self.on_status_update(f"[W{self.bot_id+1}] Error pressing key '{key}': {e}")

    def reset_projekt_camera(self):
        """Normalizes Projekt Hard camera zoom before starting the fishing loop."""
        if not self.config.get('projekt_camera_reset_enabled', False):
            return

        hold_key = str(self.config.get('projekt_camera_hold_key', '') or '').strip().lower()
        hold_seconds = float(self.config.get('projekt_camera_hold_seconds', 0.0))
        zoom_out_presses = int(self.config.get('projekt_camera_zoom_out_presses', 0))
        zoom_in_presses = int(self.config.get('projekt_camera_zoom_in_presses', 0))
        rotate_key = str(self.config.get('projekt_camera_rotate_key', '') or '').strip().lower()
        rotate_presses = int(self.config.get('projekt_camera_rotate_presses', 0))
        key_gap = float(self.config.get('projekt_camera_key_gap', 0.055))

        sequence = []
        sequence.extend(['f'] * max(0, zoom_out_presses))
        sequence.extend(['r'] * max(0, zoom_in_presses))
        if rotate_key in ('q', 'e'):
            sequence.extend([rotate_key] * max(0, rotate_presses))

        if not sequence and not (hold_key in ('f', 'r') and hold_seconds > 0):
            return

        if self.on_status_update:
            self.on_status_update(
                f"[W{self.bot_id+1}] Ajustando camara PH: "
                + (f"{hold_key.upper()} hold {hold_seconds:.1f}s, " if hold_key in ('f', 'r') and hold_seconds > 0 else "")
                + f"F x{zoom_out_presses}, R x{zoom_in_presses}"
                + (f", {rotate_key.upper()} x{rotate_presses}" if rotate_key in ('q', 'e') and rotate_presses > 0 else "")
            )

        with input_lock:
            try:
                self.window_manager.activate_window()
                time.sleep(self._t_key_set)
                if hold_key in ('f', 'r') and hold_seconds > 0:
                    if not send_scan_key(hold_key, hold_seconds):
                        self.keyboard_controller.press(hold_key)
                        time.sleep(hold_seconds)
                        self.keyboard_controller.release(hold_key)
                    time.sleep(key_gap)
                for key in sequence:
                    if self.paused or not self.running:
                        break
                    if not send_scan_key(key, self._t_key_hold):
                        self.keyboard_controller.press(key)
                        time.sleep(self._t_key_hold)
                        self.keyboard_controller.release(key)
                    time.sleep(key_gap)
            except Exception as e:
                if self.on_status_update:
                    self.on_status_update(f"[W{self.bot_id+1}] Error ajustando camara PH: {e}")

    def press_projekt_space(self, index: int, total: int):
        """Sends a Projekt Hard reel space with PH-specific hold timing."""
        if not self.keyboard_controller:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Keyboard controller not available; cannot press space")
            return

        with input_lock:
            try:
                self.window_manager.activate_window()
                time.sleep(self._t_key_set)

                if not send_scan_key('space', self._t_ph_space_hold):
                    if self.on_status_update:
                        self.on_status_update(
                            f"[W{self.bot_id+1}] SendInput failed for PH space "
                            f"(winerr {get_last_win_error()}); falling back to pynput"
                        )
                    self.keyboard_controller.press(Key.space)
                    time.sleep(self._t_ph_space_hold)
                    self.keyboard_controller.release(Key.space)

                if self.on_status_update:
                    elapsed = ""
                    if self._ph_last_bubble_decision_at:
                        elapsed = f", +{int((time.time() - self._ph_last_bubble_decision_at) * 1000)}ms desde decision"
                    self.on_status_update(
                        f"[W{self.bot_id+1}] Barra {index}/{total} enviada "
                        f"(hold {int(self._t_ph_space_hold * 1000)}ms{elapsed})"
                    )
            except Exception as e:
                if self.on_status_update:
                    self.on_status_update(f"[W{self.bot_id+1}] Error pressing PH space: {e}")

    def wait_before_projekt_reel(self):
        """Waits a humanized delay after bubble detection before first reel space."""
        if self._t_ph_pre_reel_wait <= 0:
            return
        delay = random.uniform(self._t_ph_pre_reel_wait, self._t_ph_pre_reel_wait + 0.500)
        if self.on_status_update:
            self.on_status_update(f"[W{self.bot_id+1}] Espero {delay:.2f}s antes de apretar barra")
        time.sleep(delay)

    def wait_between_projekt_spaces(self):
        """Waits a humanized delay between reel spaces."""
        if self._t_ph_space_gap_max <= 0:
            return
        delay = self._random_timing_delay(self._t_ph_space_gap_min, self._t_ph_space_gap_max)
        if self.on_status_update:
            self.on_status_update(f"[W{self.bot_id+1}] Delay entre barras: {int(delay * 1000)}ms")
        time.sleep(delay)

    def wait_after_projekt_terminal_result(self):
        """Waits after a terminal OCR result before the next bait cycle."""
        if self._t_ph_after_result_max <= 0:
            return
        delay = self._random_timing_delay(self._t_ph_after_result_min, self._t_ph_after_result_max)
        if self.on_status_update:
            self.on_status_update(f"[W{self.bot_id+1}] Delay post-OCR: {delay:.2f}s")
        time.sleep(delay)
    
    def wait_for_minigame_window(self, timeout: float = 6.0) -> bool:
        """Waits for and finds the fishing minigame window. Auto-calibrates region on first detection.
        Returns True if minigame detected, False otherwise."""
        start_time = time.time()
        
        while self.running and time.time() - start_time < timeout:
            if self.paused:
                time.sleep(0.1)
                continue
            
            try:
                # On first detection, find and calibrate the region
                if not self.region_auto_calibrated:
                    frame = self.capture_full_window()
                    bounds = self.detector.find_fishing_window_bounds(frame)
                    if bounds:
                        x, y, w, h = bounds
                        self.region = GameRegion(x, y, w, h)
                        self.region_auto_calibrated = True
                        self._update_region_cache()  # Update cached constants
                        if self.on_status_update:
                            self.on_status_update(f"[W{self.bot_id+1}] Auto-calibrated region: {w}x{h} at ({x},{y})")
                        return True
                else:
                    # Use standard detection after calibration
                    frame = self.capture_screen()
                    window_active, _ = self.detector.detect_window_and_fish(frame)
                    if window_active:
                        return True
                
                time.sleep(0.05)  # Faster polling for quicker minigame detection
            except Exception as e:
                if self.on_status_update:
                    self.on_status_update(f"[W{self.bot_id+1}] Error: {e}")
                time.sleep(0.05)
        
        return False
    
    def _scan_existing_inventory(self):
        """Scans inventory for all existing items and adds their positions to ignore list.
        Called at bot start to prevent re-processing items already in inventory.
        Uses iterative minMaxLoc with masking to find ALL distinct items (same logic as identify_item_in_inventory)."""
        templates = self._load_template_cache()
        if not templates:
            return
        
        try:
            # Activate window before capturing
            self.window_manager.activate_window(force_activate=True)
            time.sleep(0.3)  # Give window time to come into focus

            inventory_frame = self.capture_inventory_area()
            inventory_gray = cv2.cvtColor(inventory_frame, cv2.COLOR_BGR2GRAY)
            inv_h, inv_w = inventory_gray.shape

            # Local references for speed
            match_template = cv2.matchTemplate
            minMaxLoc = cv2.minMaxLoc
            TM_CCOEFF_NORMED = cv2.TM_CCOEFF_NORMED
            CONFIDENCE_THRESHOLD = 0.80

            found_count = 0
            ignored = self._ignored_positions

            for template, half_w, half_h in templates.values():
                t_h, t_w = template.shape

                if t_h > inv_h or t_w > inv_w:
                    continue

                try:
                    result = match_template(inventory_gray, template, TM_CCOEFF_NORMED)

                    # Cheap upfront rejection: if the global max is already below
                    # threshold, this template has no occurrence anywhere.
                    _, peak, _, _ = minMaxLoc(result)
                    if peak < CONFIDENCE_THRESHOLD:
                        continue

                    # Find ALL matches using iterative minMaxLoc with masking.
                    # Disambiguation is skipped here — we only need positions for the
                    # ignore-list, not species identity.
                    while True:
                        _, max_val, _, max_loc = minMaxLoc(result)
                        if max_val < CONFIDENCE_THRESHOLD:
                            break

                        pt_x, pt_y = max_loc
                        center_x = pt_x + half_w
                        center_y = pt_y + half_h

                        is_duplicate = False
                        for ix, iy in ignored:
                            if abs(center_x - ix) < 10 and abs(center_y - iy) < 10:
                                is_duplicate = True
                                break

                        if not is_duplicate:
                            ignored.add((center_x, center_y))
                            found_count += 1

                        # Mask out this match area to find the next one
                        mask_x1 = max(0, pt_x - t_w // 2)
                        mask_y1 = max(0, pt_y - t_h // 2)
                        mask_x2 = min(result.shape[1], pt_x + t_w // 2 + 1)
                        mask_y2 = min(result.shape[0], pt_y + t_h // 2 + 1)
                        result[mask_y1:mask_y2, mask_x1:mask_x2] = -1.0

                except Exception:
                    continue

            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Inventory scan: found {found_count} existing items (ignoring)")

            # Detect empty slots on current page
            self._empty_slot_positions = self._scan_empty_slots(inventory_frame)
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Found {len(self._empty_slot_positions)} empty inventory slots")

        except Exception as e:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Error scanning inventory: {e}")
    
    def _rescan_inventory_state(self):
        """Clears all slot state and rebuilds it from the currently visible inventory page.
        When auto handling is enabled, also processes any open/drop items on the page."""
        self._ignored_positions.clear()
        self._empty_slot_positions.clear()
        if self.config.get('auto_fish_handling', False):
            fish_actions = self.config.get('fish_actions', {})
            self._startup_process_page(fish_actions)
        else:
            self._scan_existing_inventory()

    def _switch_inventory_page(self):
        """Advances through remaining configured inventory pages until one with empty slots is found.
        If all pages are full, stops the bot and plays the rickroll beep."""
        while True:
            next_page = self._current_inv_page + 1
            page_num = next_page + 1  # 1-based

            if page_num > 8:
                break

            page_pos = self.config.get(f'inv_page_{page_num}_pos')
            if not page_pos:
                break

            self._current_inv_page = next_page
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Switching to inventory page {page_num}")

            with input_lock:
                self.window_manager.activate_window(force_activate=True)
                win_left, win_top, _, _ = self.window_manager.get_window_rect()
                pyautogui.moveTo(win_left + page_pos[0], win_top + page_pos[1], _pause=False)
                time.sleep(0.05)
                pyautogui.click(_pause=False)

            time.sleep(0.3)
            self._rescan_inventory_state()

            if self._empty_slot_positions:
                return  # Found a page with space — done

            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Page {page_num} also full, trying next...")

        # All configured pages are full — stop the bot
        if self.on_status_update:
            self.on_status_update(f"[W{self.bot_id+1}] All inventory pages full — stopping bot")
        self.running = False
        if self.on_bot_stop:
            self.on_bot_stop(self.bot_id)
        threading.Thread(target=play_rickroll_beep, daemon=True).start()

    def _startup_scan_and_process_all_pages(self):
        """At startup, navigate every configured inventory page and find one with empty slots.
        Auto handling on: open/drop processable items on each page before scanning.
        Auto handling off: just scan each page and mark existing items as ignored.
        After scanning all pages, navigates to the first page that has empty slots.
        If every page is full, stops the bot immediately (plays rickroll)."""
        auto = self.config.get('auto_fish_handling', False)

        if self.config.get('projekt_hard_fishing', False) and not auto:
            if self.on_status_update:
                self.on_status_update(
                    f"[W{self.bot_id+1}] Projekt Hard: omito bloqueo por inventario lleno "
                    "(manejo automatico desactivado)"
                )
            with input_lock:
                self.window_manager.activate_window(force_activate=True)
            time.sleep(0.2)
            return

        if not auto:
            # Non-auto mode: navigate pages in order, stop on the first one with empty slots.
            # No need to scan every page — we just need somewhere to put fish.
            for page_idx in range(8):
                page_num = page_idx + 1
                page_pos = self.config.get(f'inv_page_{page_num}_pos')
                if page_idx > 0 and not page_pos:
                    break  # Hit an unconfigured page — no more pages

                if self.on_status_update:
                    self.on_status_update(f"[W{self.bot_id+1}] Startup: checking inventory page {page_num}")

                if page_pos:
                    with input_lock:
                        self.window_manager.activate_window(force_activate=True)
                        win_left, win_top, _, _ = self.window_manager.get_window_rect()
                        pyautogui.moveTo(win_left + page_pos[0], win_top + page_pos[1], _pause=False)
                        time.sleep(0.05)
                        pyautogui.click(_pause=False)
                    time.sleep(0.3)
                else:
                    with input_lock:
                        self.window_manager.activate_window(force_activate=True)
                    time.sleep(0.3)

                self._current_inv_page = page_idx
                self._ignored_positions.clear()
                self._empty_slot_positions.clear()
                self._scan_existing_inventory()

                if self._empty_slot_positions:
                    return  # Found a page with space — done

            # All configured pages are full
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] All inventory pages full at startup — stopping bot")
            self.running = False
            if self.on_bot_stop:
                self.on_bot_stop(self.bot_id)
            threading.Thread(target=play_rickroll_beep, daemon=True).start()
            return

        # Auto mode: scan every page and process (open/drop) items before deciding where to settle.
        fish_actions = self.config.get('fish_actions', {})
        first_page_with_empty = None

        for page_idx in range(8):
            page_num = page_idx + 1
            page_pos = self.config.get(f'inv_page_{page_num}_pos')

            if page_idx > 0 and not page_pos:
                break

            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Startup: scanning inventory page {page_num}")

            if page_pos:
                with input_lock:
                    self.window_manager.activate_window(force_activate=True)
                    win_left, win_top, _, _ = self.window_manager.get_window_rect()
                    pyautogui.moveTo(win_left + page_pos[0], win_top + page_pos[1], _pause=False)
                    time.sleep(0.05)
                    pyautogui.click(_pause=False)
                time.sleep(0.3)
            else:
                with input_lock:
                    self.window_manager.activate_window(force_activate=True)
                time.sleep(0.3)

            self._current_inv_page = page_idx
            self._ignored_positions.clear()
            self._empty_slot_positions.clear()
            self._startup_process_page(fish_actions)

            if self._empty_slot_positions and first_page_with_empty is None:
                first_page_with_empty = page_idx

        if first_page_with_empty is None:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] All inventory pages full at startup — stopping bot")
            self.running = False
            if self.on_bot_stop:
                self.on_bot_stop(self.bot_id)
            threading.Thread(target=play_rickroll_beep, daemon=True).start()
            return

        # Navigate back to the first page that has empty slots (if we advanced past it)
        if self._current_inv_page != first_page_with_empty:
            target_page_num = first_page_with_empty + 1
            page_pos = self.config.get(f'inv_page_{target_page_num}_pos')
            if page_pos:
                if self.on_status_update:
                    self.on_status_update(
                        f"[W{self.bot_id+1}] Startup: returning to page {target_page_num} (first with empty slots)")
                with input_lock:
                    self.window_manager.activate_window(force_activate=True)
                    win_left, win_top, _, _ = self.window_manager.get_window_rect()
                    pyautogui.moveTo(win_left + page_pos[0], win_top + page_pos[1], _pause=False)
                    time.sleep(0.05)
                    pyautogui.click(_pause=False)
                time.sleep(0.3)
                self._current_inv_page = first_page_with_empty
                self._ignored_positions.clear()
                self._empty_slot_positions.clear()
                self._startup_process_page(fish_actions)

    def _startup_process_page(self, fish_actions: dict) -> None:
        """Processes a single inventory page during startup.
        Pass 1: scan all items — 'keep' items go into _ignored_positions.
        Pass 2: loop identify_item_in_inventory (which skips ignored) and call
                _startup_handle_item for each open/drop item until none remain.
        Finally rescans empty slots."""
        templates = self._load_template_cache()
        if not templates:
            return

        try:
            inventory_frame = self.capture_inventory_area()
            inventory_gray = cv2.cvtColor(inventory_frame, cv2.COLOR_BGR2GRAY)
            inv_h, inv_w = inventory_gray.shape

            match_template = cv2.matchTemplate
            minMaxLoc = cv2.minMaxLoc
            TM_CCOEFF_NORMED = cv2.TM_CCOEFF_NORMED
            CONFIDENCE_THRESHOLD = 0.80
            confusable_fish = FishingBot._confusable_fish
            keep_count = 0

            # Pass 1: collect every template hit, cluster by position, then for each
            # cluster pick the highest-confidence template as the slot's true identity.
            # Without this clustering step, a weakly-matching 'keep' template can win
            # over the actually-correct 'drop' template at the same slot purely
            # because of dict iteration order, causing the slot to be ignored.
            raw_hits = []  # (filename, cx, cy, confidence)
            for filename, (template, half_w, half_h) in templates.items():
                t_h, t_w = template.shape
                if t_h > inv_h or t_w > inv_w:
                    continue
                try:
                    result = match_template(inventory_gray, template, TM_CCOEFF_NORMED)
                    while True:
                        _, max_val, _, max_loc = minMaxLoc(result)
                        if max_val < CONFIDENCE_THRESHOLD:
                            break
                        pt_x, pt_y = max_loc
                        raw_hits.append((filename, pt_x + half_w, pt_y + half_h, max_val))
                        mask_x1 = max(0, pt_x - t_w // 2)
                        mask_y1 = max(0, pt_y - t_h // 2)
                        mask_x2 = min(result.shape[1], pt_x + t_w // 2 + 1)
                        mask_y2 = min(result.shape[0], pt_y + t_h // 2 + 1)
                        result[mask_y1:mask_y2, mask_x1:mask_x2] = -1.0
                except Exception:
                    continue

            # Cluster hits within 10 px → one entry per physical slot, keeping
            # the best (filename, conf) for that slot.
            slot_best = []  # list of [cx, cy, best_filename, best_conf]
            for filename, cx, cy, conf in raw_hits:
                merged = False
                for slot in slot_best:
                    if abs(cx - slot[0]) < 10 and abs(cy - slot[1]) < 10:
                        if conf > slot[3]:
                            slot[2] = filename
                            slot[3] = conf
                        merged = True
                        break
                if not merged:
                    slot_best.append([cx, cy, filename, conf])

            # Now classify each slot by its best-matching template's action.
            for cx, cy, best_filename, _ in slot_best:
                if any(abs(cx - ix) < 10 and abs(cy - iy) < 10
                       for ix, iy in self._ignored_positions):
                    continue
                matched_filename = best_filename
                if best_filename in confusable_fish:
                    matched_filename = self._disambiguate_confusable_fish(
                        inventory_frame, cx, cy, best_filename)
                if fish_actions.get(matched_filename, 'keep') == 'keep':
                    self._ignored_positions.add((cx, cy))
                    keep_count += 1

            if self.on_status_update and keep_count:
                self.on_status_update(f"[W{self.bot_id+1}] Startup: {keep_count} 'keep' items (ignored)")

            # Pass 2: open/drop everything else (identify_item skips _ignored_positions)
            processed_count = 0
            max_items = 50  # Safety cap against infinite loops
            while processed_count < max_items:
                inventory_frame = self.capture_inventory_area()
                match = self.identify_item_in_inventory(inventory_frame, ignore_positions=self._ignored_positions)
                if not match:
                    break
                filename, (inv_x, inv_y) = match
                action = fish_actions.get(filename, 'keep')
                if action == 'keep':
                    self._ignored_positions.add((inv_x, inv_y))
                    continue
                self._startup_handle_item(filename, inv_x, inv_y, action)
                processed_count += 1

            if self.on_status_update and processed_count:
                self.on_status_update(f"[W{self.bot_id+1}] Startup: processed {processed_count} open/drop items")

            # Final empty-slot scan after all items have been handled
            inventory_frame = self.capture_inventory_area()
            self._empty_slot_positions = self._scan_empty_slots(inventory_frame)
            if self.on_status_update:
                self.on_status_update(
                    f"[W{self.bot_id+1}] Startup: {len(self._empty_slot_positions)} empty slots on this page")

        except Exception as e:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Error processing inventory page: {e}")

    def _startup_handle_item(self, filename: str, inv_x: int, inv_y: int, action: str) -> None:
        """Executes open/drop for a single item found during startup scan.
        Mirrors handle_caught_item() — single lock covers the entire sequence so
        no other bot can interleave mouse operations mid-action."""
        fish_name = filename.replace('_living.jpg', '').replace('_item.jpg', '')
        try:
            with input_lock:
                self.window_manager.activate_window(force_activate=True)
                win_left, win_top, win_width, win_height = self.window_manager.get_window_rect()
                screen_x = win_left + win_width - self._inventory_width + inv_x
                screen_y = win_top + self._inventory_y_offset + inv_y
                win_cx = win_left + win_width // 2
                win_cy = win_top + win_height // 2

                if action == 'open':
                    if self.on_status_update:
                        self.on_status_update(f"[W{self.bot_id+1}] Startup opening: {fish_name}")

                    pyautogui.moveTo(screen_x, screen_y, _pause=False)
                    time.sleep(0.05)
                    pyautogui.click(button='right', _pause=False)
                    time.sleep(self._t_open_wait)
                    pyautogui.moveTo(win_cx, win_cy, _pause=False)

                    time.sleep(self._t_open_wait)
                    still_there = self._is_item_at_position(self.capture_inventory_area(), inv_x, inv_y)
                    if still_there:
                        time.sleep(self._t_dead_check)
                        if self._is_item_at_position(self.capture_inventory_area(), inv_x, inv_y):
                            self._ignored_positions.add((inv_x, inv_y))

                elif action == 'drop':
                    confirm_pos = self.config.get('confirm_button_pos')
                    if not confirm_pos:
                        self._ignored_positions.add((inv_x, inv_y))
                        return

                    if self.on_status_update:
                        self.on_status_update(f"[W{self.bot_id+1}] Startup dropping: {fish_name}")

                    is_fish = '_living' in filename
                    still_there = True

                    if is_fish:
                        pyautogui.moveTo(screen_x, screen_y, _pause=False)
                        time.sleep(0.05)
                        pyautogui.click(button='right', _pause=False)
                        time.sleep(self._t_open_wait)
                        pyautogui.moveTo(win_cx, win_cy, _pause=False)

                        time.sleep(self._t_open_wait)
                        still_there = self._is_item_at_position(self.capture_inventory_area(), inv_x, inv_y)

                    if still_there:
                        drop_pos = self.config.get('drop_button_pos')

                        pyautogui.moveTo(screen_x, screen_y, _pause=False)
                        time.sleep(np.random.uniform(0.05, 0.07))
                        pyautogui.click(_pause=False)
                        time.sleep(self._t_drop_settle)

                        pyautogui.moveTo(win_cx, win_cy, _pause=False)
                        time.sleep(np.random.uniform(0.05, 0.07))
                        pyautogui.click(_pause=False)
                        time.sleep(self._t_drop_settle)

                        if drop_pos:
                            pyautogui.moveTo(win_left + drop_pos[0], win_top + drop_pos[1], _pause=False)
                            time.sleep(np.random.uniform(0.05, 0.07))
                            pyautogui.click(_pause=False)
                            time.sleep(self._t_drop_settle)

                        pyautogui.moveTo(win_left + confirm_pos[0], win_top + confirm_pos[1], _pause=False)
                        time.sleep(np.random.uniform(0.05, 0.07))
                        pyautogui.click(_pause=False)
                        time.sleep(self._t_drop_settle)

                        pyautogui.moveTo(win_cx, win_cy, _pause=False)

                        # Verify drop succeeded while still holding lock
                        time.sleep(self._t_drop_settle)
                        if self._is_item_at_position(self.capture_inventory_area(), inv_x, inv_y):
                            self._ignored_positions.add((inv_x, inv_y))

        except Exception as e:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Error in startup item handler: {e}")
            self._ignored_positions.add((inv_x, inv_y))

    def _load_classic_fish_template(self):
        """Loads the classic_fish.jpg template for classic fishing mode."""
        if FishingBot._classic_fish_template is not None:
            return FishingBot._classic_fish_template
        
        template_path = get_resource_path("classic_fish.jpg")
        if not os.path.exists(template_path):
            # Try .png extension
            template_path = get_resource_path("classic_fish.png")
        
        if os.path.exists(template_path):
            try:
                template = cv2.imread(template_path)
                if template is not None:
                    FishingBot._classic_fish_template = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
                    if self.on_status_update:
                        self.on_status_update(f"[W{self.bot_id+1}] Loaded classic fish template")
            except Exception as e:
                if self.on_status_update:
                    self.on_status_update(f"[W{self.bot_id+1}] Error loading classic fish template: {e}")
        else:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Classic fish template not found at assets/classic_fish.jpg")
        
        return FishingBot._classic_fish_template

    def _load_projekt_hard_bubble_templates(self) -> Dict[int, tuple]:
        """Loads Projekt Hard bubble templates keyed by required space presses."""
        if FishingBot._projekt_hard_bubble_templates is not None:
            return FishingBot._projekt_hard_bubble_templates

        templates = {}
        for count in (1, 2, 3):
            path = get_resource_path(f"projekt_hard_bubble_{count}.png")
            if not os.path.exists(path):
                continue
            img = cv2.imread(path)
            if img is None:
                continue
            # Mask out grass/character background. Keep the white speech bubble,
            # black outline, and orange number/rod pixels so matching survives
            # camera/background changes.
            b, g, r = cv2.split(img)
            white = (r > 185) & (g > 185) & (b > 185)
            dark = (r < 75) & (g < 75) & (b < 75)
            orange = (r > 145) & (g > 80) & (g < 190) & (b < 120)
            mask = np.where(white | dark | orange, 255, 0).astype(np.uint8)
            if cv2.countNonZero(mask) < 100:
                mask = np.full(img.shape[:2], 255, dtype=np.uint8)
            templates[count] = (img, mask)

        FishingBot._projekt_hard_bubble_templates = templates
        if self.on_status_update:
            self.on_status_update(f"[W{self.bot_id+1}] Loaded {len(templates)} Projekt Hard bubble templates")
        return templates

    def _load_projekt_hard_number_templates(self) -> Dict[int, list]:
        """Loads Projekt Hard orange-number masks keyed by required space presses."""
        if FishingBot._projekt_hard_number_templates is not None:
            return FishingBot._projekt_hard_number_templates

        templates = {}
        assets_dir = get_resource_path("assets")
        for count in (1, 2, 3):
            number_templates = []
            prefixes = (f"projekt_hard_number_{count}.png", f"projekt_hard_number_{count}_")
            for filename in sorted(os.listdir(assets_dir)):
                if filename == prefixes[0] or filename.startswith(prefixes[1]):
                    path = os.path.join(assets_dir, filename)
                    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
                    if img is None:
                        continue
                    _, mask = cv2.threshold(img, 40, 255, cv2.THRESH_BINARY)
                    number_templates.append(mask)
            if number_templates:
                templates[count] = number_templates

        FishingBot._projekt_hard_number_templates = templates
        if self.on_status_update:
            total = sum(len(v) for v in templates.values())
            self.on_status_update(f"[W{self.bot_id+1}] Loaded {total} Projekt Hard number templates")
        return templates

    def _load_projekt_hard_knn_model(self):
        """Loads the trained lightweight Projekt Hard digit OCR vectors."""
        if FishingBot._projekt_hard_knn_model is not None:
            return FishingBot._projekt_hard_knn_model

        path = os.path.join(get_resource_path("assets"), "projekt_hard_number_knn.npz")
        if not os.path.exists(path):
            FishingBot._projekt_hard_knn_model = False
            return None
        try:
            data = np.load(path)
            features = data["features"].astype(np.float32)
            labels = data["labels"].astype(np.int32)
            if features.ndim != 2 or labels.ndim != 1 or len(features) != len(labels):
                FishingBot._projekt_hard_knn_model = False
                return None
            FishingBot._projekt_hard_knn_model = (features, labels)
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Loaded Projekt Hard OCR KNN ({len(labels)} samples)")
            return FishingBot._projekt_hard_knn_model
        except Exception as e:
            FishingBot._projekt_hard_knn_model = False
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Projekt Hard OCR KNN load error: {e}")
            return None

    def _load_projekt_hard_permit_template(self):
        """Loads optional Projekt Hard fishing permit template if present."""
        if FishingBot._projekt_hard_permit_template is not None:
            return FishingBot._projekt_hard_permit_template

        path = get_resource_path("projekt_hard_permit.png")
        if not os.path.exists(path):
            FishingBot._projekt_hard_permit_template = False
            return None

        img = cv2.imread(path)
        if img is None:
            FishingBot._projekt_hard_permit_template = False
            return None

        FishingBot._projekt_hard_permit_template = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return FishingBot._projekt_hard_permit_template

    def has_projekt_hard_fishing_permit(self) -> bool:
        """Checks the optional fishing permit template in the visible game window."""
        template = self._load_projekt_hard_permit_template()
        if template is None:
            return True

        try:
            frame = self.capture_full_window()
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            h, w = gray.shape
            right_side = gray[:, max(0, w - 260):w]
            t_h, t_w = template.shape
            if t_h > right_side.shape[0] or t_w > right_side.shape[1]:
                return False
            result = cv2.matchTemplate(right_side, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            return max_val >= 0.75
        except Exception:
            return True

    def detect_projekt_hard_bubble(self) -> Tuple[int, float]:
        """Returns (space_press_count, confidence) for the visible Projekt Hard bubble."""
        templates = self._load_projekt_hard_bubble_templates()
        if not templates:
            return (0, 0.0)

        try:
            frame = self.capture_full_window()
            h, w = frame.shape[:2]

            # Projekt Hard is usually played in an 800x600-ish client. The fish
            # bubble appears around the player, not in the top water area where
            # rods/background produce strong false template matches.
            crop_left = max(0, int(w * 0.02))
            crop_right = min(w, int(w * 0.78))
            crop_top = max(0, int(h * 0.16))
            crop_bottom = min(h, int(h * 0.88))
            search = frame[crop_top:crop_bottom, crop_left:crop_right]
            color_location, color_shape, color_confidence = self._find_projekt_hard_bubble_by_color(search)

            best_count = 0
            best_confidence = 0.0
            best_location = None
            best_template_shape = None
            scores = {}
            if color_location is not None and color_shape is not None:
                best_location = color_location
                best_template_shape = color_shape
                x, y = color_location
                t_h, t_w = color_shape
                bubble_crop = search[y:y + t_h, x:x + t_w]
                best_count, best_confidence, scores = self._classify_projekt_hard_number(bubble_crop)
            else:
                for count, (template, mask) in templates.items():
                    t_h, t_w = template.shape[:2]
                    if t_h > search.shape[0] or t_w > search.shape[1]:
                        continue
                    result = cv2.matchTemplate(search, template, cv2.TM_CCORR_NORMED, mask=mask)
                    _, max_val, _, max_loc = cv2.minMaxLoc(result)
                    if max_val >= 0.70 and not self._projekt_hard_candidate_has_bubble_colors(
                        search,
                        max_loc,
                        (t_h, t_w),
                    ):
                        max_val = 0.0
                    scores[count] = float(max_val)
                    if max_val > best_confidence:
                        best_confidence = max_val
                        best_count = count
                        best_location = max_loc
                        best_template_shape = (t_h, t_w)

            self._last_ph_scores = scores
            self._last_ph_color_confidence = color_confidence
            self._last_ph_best_count = best_count
            self._last_ph_best_confidence = best_confidence
            visible, visible_source, visible_confidence = self._projekt_hard_bubble_is_visible(
                color_confidence,
                best_confidence,
            )
            self._last_ph_visible_confidence = visible_confidence
            self._last_ph_visible_source = visible_source
            now = time.time()
            if (
                self.config.get('projekt_hard_debug_only', False)
                and not visible
                and now - self._last_ph_score_log >= 1.0
            ):
                self._last_ph_score_log = now
                score_text = ", ".join(f"{k}:{v:.2f}" for k, v in sorted(scores.items()))
                if self.on_status_update:
                    self.on_status_update(
                        f"[W{self.bot_id+1}] PH scores [{score_text}] "
                        f"best={best_count}:{best_confidence:.2f} color={color_confidence:.2f} "
                        f"visible={visible_source}:{visible_confidence:.2f}"
                    )

            if (self.config.get('projekt_hard_debug_only', False) or best_confidence >= 0.45) and now - self._last_ph_debug_save >= 2.0:
                self._last_ph_debug_save = now
                try:
                    cv2.imwrite(get_resource_path("projekt_hard_debug_search.png"), search)
                except Exception:
                    pass

            if (
                self.config.get('projekt_hard_debug_only', False)
                and color_confidence >= 0.40
                and color_location is not None
                and color_shape is not None
                and not self._ph_sample_active
                and now - self._last_ph_sample_save >= 1.0
            ):
                self._last_ph_sample_save = now
                self._ph_sample_active = True
                self._save_projekt_hard_bubble_sample(
                    search,
                    color_location,
                    color_shape,
                    best_count,
                    color_confidence,
                )
            elif not visible:
                self._ph_sample_active = False

            number_confident = self._projekt_hard_number_is_confident(best_count, best_confidence, scores)
            template_confident = self._projekt_hard_template_choice_is_confident(best_count, best_confidence, scores)
            if visible and (
                (visible_source == "color" and number_confident)
                or (visible_source == "template" and template_confident)
            ):
                if color_location is not None and color_shape is not None:
                    self._save_projekt_hard_detection_crop(
                        search,
                        color_location,
                        color_shape,
                        best_count,
                        best_confidence,
                        color_confidence,
                        scores,
                    )
                return (best_count, best_confidence)
            return (0, max(best_confidence, color_confidence, visible_confidence))
        except Exception as e:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Projekt Hard detection error: {e}")
            return (0, 0.0)

    def _projekt_hard_number_is_confident(self, count: int, confidence: float, scores: Dict[int, float]) -> bool:
        """Returns True when a single-frame number read is strong enough."""
        if not count or confidence < 0.25:
            return False
        ordered = sorted(scores.values(), reverse=True)
        second = ordered[1] if len(ordered) > 1 else 0.0
        margin = confidence - second
        return (
            confidence >= 0.50
            or (confidence >= 0.40 and margin >= 0.12)
            or (confidence >= 0.36 and margin >= 0.08)
            or (confidence >= 0.38 and margin >= 0.035)
        )

    def _projekt_hard_template_choice_is_confident(self, count: int, confidence: float, scores: Dict[int, float]) -> bool:
        """Returns True when full-bubble templates agree on a count, not just visibility."""
        if not count or confidence < 0.72:
            return False
        ordered = sorted(scores.values(), reverse=True)
        second = ordered[1] if len(ordered) > 1 else 0.0
        return confidence - second >= 0.08

    def _projekt_hard_bubble_is_visible(self, color_confidence: float, template_confidence: float) -> Tuple[bool, str, float]:
        """Separates bubble visibility from the number read."""
        if color_confidence >= 0.40:
            return (True, "color", color_confidence)
        if template_confidence >= 0.70:
            return (True, "template", template_confidence)
        return (False, "none", max(color_confidence, template_confidence))

    def observe_projekt_hard_bubble(
        self,
        timeout: float = 40.0,
        baseline_text: str = "",
        use_ocr_abort: bool = True,
        perform_reel_delay: bool = True,
    ) -> Tuple[int, float]:
        """Waits for a bubble, samples it until it disappears, then returns a stable number."""
        start_time = time.time()
        best_seen = (0, 0.0)
        last_ocr_poll = 0.0
        last_chat_text = baseline_text or ""
        self._ph_wait_abort_state = ""
        self._ph_recent_frames.clear()
        last_buffer_capture = 0.0

        while self.running and time.time() - start_time < timeout:
            if self.paused:
                time.sleep(0.1)
                start_time += 0.1
                continue

            self.detect_projekt_hard_bubble()
            now_for_buffer = time.time()
            if now_for_buffer - last_buffer_capture >= 0.25:
                last_buffer_capture = now_for_buffer
                try:
                    frame = self.capture_full_window()
                    h, w = frame.shape[:2]
                    crop_left = max(0, int(w * 0.02))
                    crop_right = min(w, int(w * 0.78))
                    crop_top = max(0, int(h * 0.16))
                    crop_bottom = min(h, int(h * 0.88))
                    search = frame[crop_top:crop_bottom, crop_left:crop_right]
                    self._ph_recent_frames.append((now_for_buffer, frame.copy(), search.copy()))
                except Exception:
                    pass
            color_confidence = self._last_ph_color_confidence
            visible_confidence = self._last_ph_visible_confidence
            visible_source = self._last_ph_visible_source
            template_start_is_confident = self._projekt_hard_template_choice_is_confident(
                self._last_ph_best_count,
                self._last_ph_best_confidence,
                dict(self._last_ph_scores),
            )
            if visible_source == "none" or (visible_source == "template" and not template_start_is_confident):
                best_seen = max(best_seen, (0, visible_confidence), key=lambda item: item[1])
                now = time.time()
                if self.on_status_update and now - self._last_ph_wait_log >= 2.0:
                    self._last_ph_wait_log = now
                    elapsed = now - start_time
                    self.on_status_update(
                        f"[W{self.bot_id+1}] Buscando globo... {elapsed:.1f}s/"
                        f"{timeout:.1f}s color={color_confidence:.2f} "
                        f"visible={visible_source}:{visible_confidence:.2f}"
                    )
                if use_ocr_abort and self._projekt_ocr_available() and now - last_ocr_poll >= 0.7:
                    last_ocr_poll = now
                    text = self.read_projekt_chat_text()
                    if text and text != last_chat_text:
                        if self.check_projekt_private_messages(text, baseline_text=baseline_text):
                            self._ph_wait_abort_state = "private_message"
                            return (0, best_seen[1])
                        last_chat_text = text
                        state = self.classify_projekt_chat_text(text)
                        last_line = self.get_last_projekt_fishing_line(text)
                        if self.on_status_update:
                            preview = last_line[-140:] if len(last_line) > 140 else last_line
                            self.on_status_update(f"[W{self.bot_id+1}] OCR durante globo: {state} | {preview}")
                        if state in ("too_late", "wrong_presses", "no_bait", "no_permit"):
                            self._ph_wait_abort_state = state
                            if state in ("too_late", "wrong_presses"):
                                self._save_projekt_hard_recent_miss_frames(state)
                            return (0, best_seen[1])
                time.sleep(0.05)
                continue

            totals = {1: 0.0, 2: 0.0, 3: 0.0}
            hits = {1: 0, 2: 0, 3: 0}
            best_frame = (0, 0.0)
            visible_start = time.time()
            missing_frames = 0
            seed_count = self._last_ph_best_count
            seed_confidence = self._last_ph_best_confidence
            seed_scores = dict(self._last_ph_scores)
            if (
                (visible_source == "color" and self._projekt_hard_number_is_confident(seed_count, seed_confidence, seed_scores))
                or (visible_source == "template" and self._projekt_hard_template_choice_is_confident(seed_count, seed_confidence, seed_scores))
            ):
                totals[seed_count] += seed_confidence
                hits[seed_count] += 1
                best_frame = (seed_count, seed_confidence)
                best_seen = max(best_seen, best_frame, key=lambda item: item[1])
            if self.on_status_update:
                self.on_status_update(
                    f"[W{self.bot_id+1}] Globo PH detectado; analizando frames "
                    f"(via {visible_source}:{visible_confidence:.2f})..."
                )

            while self.running and time.time() - visible_start < 3.5:
                if self.paused:
                    time.sleep(0.1)
                    visible_start += 0.1
                    continue

                self.detect_projekt_hard_bubble()
                color_confidence = self._last_ph_color_confidence
                visible_confidence = self._last_ph_visible_confidence
                visible_source = self._last_ph_visible_source
                scores = dict(self._last_ph_scores)
                count = self._last_ph_best_count
                confidence = self._last_ph_best_confidence

                if visible_source == "none":
                    missing_frames += 1
                    if missing_frames >= 2:
                        break
                    time.sleep(0.05)
                    continue
                missing_frames = 0

                if (
                    (visible_source == "color" and self._projekt_hard_number_is_confident(count, confidence, scores))
                    or (visible_source == "template" and self._projekt_hard_template_choice_is_confident(count, confidence, scores))
                ):
                    ordered = sorted(scores.values(), reverse=True)
                    second = ordered[1] if len(ordered) > 1 else 0.0
                    margin = max(0.0, confidence - second)
                    weight = confidence + (margin * 0.75)
                    totals[count] += weight
                    hits[count] += 1
                    if confidence > best_frame[1]:
                        best_frame = (count, confidence)

                if confidence > best_seen[1]:
                    best_seen = (count, confidence)

                time.sleep(0.05)

            final_count = max(totals, key=totals.get)
            final_score = totals[final_count]
            other_score = max((score for count, score in totals.items() if count != final_count), default=0.0)
            total_hits = sum(hits.values())
            other_hits = max((hit_count for count, hit_count in hits.items() if count != final_count), default=0)
            visible_elapsed = time.time() - visible_start
            temporal_margin = final_score - other_score
            temporal_majority = (
                total_hits >= 6
                and hits[final_count] > other_hits
                and (
                    temporal_margin >= 0.30
                    or (
                        hits[final_count] >= other_hits + 2
                        and final_score >= other_score * 1.18
                    )
                )
            )

            if (
                total_hits >= 1
                and (
                    (
                        final_count == best_frame[0]
                        and (
                            best_frame[1] >= 0.50
                            or temporal_margin >= 0.25
                            or (hits[final_count] >= 2 and final_score >= other_score * 1.45)
                        )
                    )
                    or temporal_majority
                )
            ):
                stable_confidence = max(best_frame[1], final_score / max(1, hits[final_count]))
                if self.on_status_update:
                    self.on_status_update(
                        f"[W{self.bot_id+1}] Veredicto globo PH: {final_count} barra(s) | "
                        f"analizado {visible_elapsed:.2f}s | mejor frame={best_frame[1]:.2f} | "
                        f"conf={stable_confidence:.2f} | votos={hits} | margen={temporal_margin:.2f}"
                    )
                self._ph_last_bubble_decision_at = time.time()
                return (final_count, stable_confidence)

            if self.on_status_update:
                self.on_status_update(
                    f"[W{self.bot_id+1}] Veredicto globo PH: inconcluso | "
                    f"totales={ {k: round(v, 2) for k, v in totals.items()} } | votos={hits}"
                )
            return (0, best_seen[1])

        return (0, best_seen[1])

    def _find_projekt_hard_bubble_by_color(self, search_frame):
        """Finds the visible bubble by its white circle and orange number/fish pixels."""
        if search_frame.size == 0:
            return (None, None, 0.0)

        b, g, r = cv2.split(search_frame)
        white = ((r > 165) & (g > 165) & (b > 155)).astype(np.uint8) * 255
        orange = ((r > 120) & (g > 45) & (g < 205) & (b < 175) & (r > b + 20)).astype(np.uint8) * 255

        kernel = np.ones((3, 3), dtype=np.uint8)
        white = cv2.morphologyEx(white, cv2.MORPH_CLOSE, kernel, iterations=1)
        contours, _ = cv2.findContours(white, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        best = (None, None, 0.0)
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < 180 or area > 1800:
                continue

            x, y, width, height = cv2.boundingRect(contour)
            if width < 18 or height < 18 or width > 70 or height > 70:
                continue

            aspect = width / float(height)
            if aspect < 0.60 or aspect > 1.70:
                continue

            pad = 12
            x1 = max(0, x - pad)
            y1 = max(0, y - pad)
            x2 = min(search_frame.shape[1], x + width + pad)
            y2 = min(search_frame.shape[0], y + height + pad)
            white_pixels = int(np.count_nonzero(white[y1:y2, x1:x2]))
            orange_pixels = int(np.count_nonzero(orange[y1:y2, x1:x2]))

            if white_pixels < 70 or orange_pixels < 3:
                continue

            circularity_score = min(width, height) / float(max(width, height))
            color_score = min(1.0, (white_pixels / 420.0) * 0.7 + (orange_pixels / 30.0) * 0.3)
            confidence = max(0.0, min(1.0, color_score * circularity_score))
            if confidence > best[2]:
                best = ((x1, y1), (y2 - y1, x2 - x1), confidence)

        return best

    def _extract_projekt_hard_number_mask(self, bubble_crop):
        """Extracts a normalized binary mask from the orange number area."""
        if bubble_crop is None or bubble_crop.size == 0:
            return None

        h, w = bubble_crop.shape[:2]
        roi = bubble_crop[
            max(0, int(h * 0.06)):min(h, int(h * 0.68)),
            max(0, int(w * 0.28)):min(w, int(w * 0.96)),
        ]
        if roi.size == 0:
            return None

        b, g, r = cv2.split(roi)
        mask = ((r > 135) & (g > 60) & (g < 205) & (b < 170)).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), dtype=np.uint8))

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        useful = np.zeros_like(mask)
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < 4 or area > 420:
                continue
            x, y, width, height = cv2.boundingRect(contour)
            if width > mask.shape[1] * 0.70 or height > mask.shape[0] * 0.90:
                continue
            center_y = y + (height / 2.0)
            # The fish tail/rod highlight is usually lower-right. Keeping the
            # upper/middle orange components avoids reading that tail as a "3".
            if center_y > mask.shape[0] * 0.78:
                continue
            cv2.drawContours(useful, [contour], -1, 255, thickness=cv2.FILLED)
        if cv2.countNonZero(useful) >= 12:
            mask = useful

        ys, xs = np.where(mask > 0)
        if len(xs) < 20:
            return None

        x1 = max(0, int(xs.min()) - 3)
        x2 = min(mask.shape[1], int(xs.max()) + 4)
        y1 = max(0, int(ys.min()) - 3)
        y2 = min(mask.shape[0], int(ys.max()) + 4)
        mask = mask[y1:y2, x1:x2]
        if mask.size == 0:
            return None

        return cv2.resize(mask, (48, 64), interpolation=cv2.INTER_NEAREST)

    def _normalize_projekt_hard_digit_mask(self, mask):
        """Centers the extracted digit mask so KNN sees shape, not crop position."""
        if mask is None or mask.size == 0:
            return None
        binary = (mask > 0).astype(np.uint8) * 255
        ys, xs = np.where(binary > 0)
        if len(xs) < 18:
            return None

        x1 = max(0, int(xs.min()) - 2)
        x2 = min(binary.shape[1], int(xs.max()) + 3)
        y1 = max(0, int(ys.min()) - 2)
        y2 = min(binary.shape[0], int(ys.max()) + 3)
        binary = binary[y1:y2, x1:x2]
        if binary.size == 0:
            return None

        out = np.zeros((64, 48), dtype=np.uint8)
        h, w = binary.shape
        scale = min(40.0 / max(1, w), 56.0 / max(1, h))
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        resized = cv2.resize(binary, (new_w, new_h), interpolation=cv2.INTER_NEAREST)
        ox = (48 - new_w) // 2
        oy = (64 - new_h) // 2
        out[oy:oy + new_h, ox:ox + new_w] = resized
        return out

    def _projekt_hard_digit_feature(self, mask):
        """Feature vector used by tools/train_project_hard_ocr.py."""
        norm = self._normalize_projekt_hard_digit_mask(mask)
        if norm is None:
            return None
        small = cv2.resize(norm, (24, 32), interpolation=cv2.INTER_AREA)
        binary = (small > 40).astype(np.float32)
        parts = [
            binary.reshape(-1),
            binary.sum(axis=0) / 32.0,
            binary.sum(axis=1) / 24.0,
        ]
        grid = []
        for gy in range(4):
            for gx in range(3):
                cell = binary[gy * 8:(gy + 1) * 8, gx * 8:(gx + 1) * 8]
                grid.append(float(cell.mean()))
        parts.append(np.array(grid, dtype=np.float32))
        vector = np.concatenate(parts).astype(np.float32)
        norm_value = float(np.linalg.norm(vector))
        if norm_value > 0:
            vector /= norm_value
        return vector

    def _classify_projekt_hard_number_knn(self, candidate) -> Tuple[int, float, Dict[int, float]]:
        """Classifies the digit with the trained local KNN OCR dataset."""
        model = self._load_projekt_hard_knn_model()
        if not model:
            return (0, 0.0, {count: 0.0 for count in (1, 2, 3)})

        feature = self._projekt_hard_digit_feature(candidate)
        if feature is None:
            return (0, 0.0, {count: 0.0 for count in (1, 2, 3)})

        features, labels = model
        if features.shape[1] != feature.shape[0]:
            return (0, 0.0, {count: 0.0 for count in (1, 2, 3)})

        distances = np.linalg.norm(features - feature.reshape(1, -1), axis=1)
        k = min(11, len(distances))
        nearest = np.argpartition(distances, k - 1)[:k]
        votes = {1: 0.0, 2: 0.0, 3: 0.0}
        for idx in nearest:
            label = int(labels[idx])
            if label not in votes:
                continue
            votes[label] += 1.0 / (float(distances[idx]) + 0.001)

        total = sum(votes.values())
        if total <= 0:
            return (0, 0.0, {count: 0.0 for count in (1, 2, 3)})
        scores = {count: votes[count] / total for count in (1, 2, 3)}
        best_count = max(scores, key=scores.get)
        return (best_count, scores[best_count], scores)

    def _shifted_mask_iou(self, candidate_bool, template_bool) -> float:
        """Best IoU after small translations so moving numbers still match."""
        if candidate_bool.shape != template_bool.shape:
            template_bool = cv2.resize(
                template_bool.astype(np.uint8) * 255,
                (candidate_bool.shape[1], candidate_bool.shape[0]),
                interpolation=cv2.INTER_NEAREST,
            ) > 0

        best = 0.0
        height, width = candidate_bool.shape
        for dy in range(-8, 9, 2):
            for dx in range(-8, 9, 2):
                src_x1 = max(0, -dx)
                src_x2 = min(width, width - dx)
                dst_x1 = max(0, dx)
                dst_x2 = min(width, width + dx)
                src_y1 = max(0, -dy)
                src_y2 = min(height, height - dy)
                dst_y1 = max(0, dy)
                dst_y2 = min(height, height + dy)
                if src_x1 >= src_x2 or src_y1 >= src_y2:
                    continue

                shifted = np.zeros_like(candidate_bool)
                shifted[dst_y1:dst_y2, dst_x1:dst_x2] = candidate_bool[src_y1:src_y2, src_x1:src_x2]
                intersection = np.count_nonzero(shifted & template_bool)
                union = np.count_nonzero(shifted | template_bool)
                score = float(intersection / union) if union else 0.0
                if score > best:
                    best = score
        return best

    def _mask_chamfer_similarity(self, candidate_bool, template_bool) -> float:
        """Shape similarity based on contour distance, useful when the number shifts or thins."""
        if candidate_bool.shape != template_bool.shape:
            template_bool = cv2.resize(
                template_bool.astype(np.uint8) * 255,
                (candidate_bool.shape[1], candidate_bool.shape[0]),
                interpolation=cv2.INTER_NEAREST,
            ) > 0

        if np.count_nonzero(candidate_bool) < 5 or np.count_nonzero(template_bool) < 5:
            return 0.0

        candidate_inv = (~candidate_bool).astype(np.uint8)
        template_inv = (~template_bool).astype(np.uint8)
        candidate_dt = cv2.distanceTransform(candidate_inv, cv2.DIST_L2, 3)
        template_dt = cv2.distanceTransform(template_inv, cv2.DIST_L2, 3)

        candidate_to_template = float(template_dt[candidate_bool].mean())
        template_to_candidate = float(candidate_dt[template_bool].mean())
        mean_distance = (candidate_to_template + template_to_candidate) / 2.0
        return float(1.0 / (1.0 + (mean_distance / 3.0)))

    def _classify_projekt_hard_number(self, bubble_crop) -> Tuple[int, float, Dict[int, float]]:
        """Classifies a Projekt Hard bubble crop as 1, 2, or 3 presses."""
        templates = self._load_projekt_hard_number_templates()
        candidate = self._extract_projekt_hard_number_mask(bubble_crop)
        if candidate is None or not templates:
            return (0, 0.0, {count: 0.0 for count in (1, 2, 3)})

        candidate_bool = candidate > 0
        candidate_pixels = int(np.count_nonzero(candidate_bool))
        if candidate_pixels < 80 or candidate_pixels > 900:
            return (0, 0.0, {count: 0.0 for count in (1, 2, 3)})

        scores = {}
        best_count = 0
        best_score = 0.0
        for count, template_list in templates.items():
            iou_score = 0.0
            chamfer_score = 0.0
            for template in template_list:
                template_bool = template > 0
                template_score = self._shifted_mask_iou(candidate_bool, template_bool)
                if template_score > iou_score:
                    iou_score = template_score
                shape_score = self._mask_chamfer_similarity(candidate_bool, template_bool)
                if shape_score > chamfer_score:
                    chamfer_score = shape_score

            # IoU remains the primary signal. Chamfer acts like a tiny OCR
            # fallback for shifted/thin 2/3 masks without overpowering clean 1s.
            score = max(iou_score, chamfer_score * 0.62)
            scores[count] = score
            if score > best_score:
                best_score = score
                best_count = count

        knn_count, knn_confidence, knn_scores = self._classify_projekt_hard_number_knn(candidate)
        if knn_count:
            ordered_knn = sorted(knn_scores.values(), reverse=True)
            knn_second = ordered_knn[1] if len(ordered_knn) > 1 else 0.0
            knn_margin = knn_confidence - knn_second
            if knn_confidence >= 0.42 and knn_margin >= 0.08:
                for count in (1, 2, 3):
                    scores[count] = max(scores.get(count, 0.0), knn_scores.get(count, 0.0) * 0.72)
                if scores.get(knn_count, 0.0) > best_score:
                    best_count = knn_count
                    best_score = scores[knn_count]

        return (best_count, best_score, scores)

    def _projekt_hard_candidate_has_bubble_colors(
        self,
        search_frame,
        location: Tuple[int, int],
        template_shape: Tuple[int, int],
    ) -> bool:
        """Rejects rod/terrain matches that do not look like a fish bubble."""
        x, y = location
        t_h, t_w = template_shape
        pad = 8
        y1 = max(0, y - pad)
        y2 = min(search_frame.shape[0], y + t_h + pad)
        x1 = max(0, x - pad)
        x2 = min(search_frame.shape[1], x + t_w + pad)
        crop = search_frame[y1:y2, x1:x2]
        if crop.size == 0:
            return False

        b, g, r = cv2.split(crop)
        white = (r > 190) & (g > 190) & (b > 190)
        orange = (r > 145) & (g > 70) & (g < 190) & (b < 135)
        white_pixels = int(np.count_nonzero(white))
        orange_pixels = int(np.count_nonzero(orange))

        return white_pixels >= 45 and orange_pixels >= 8

    def _save_projekt_hard_bubble_sample(
        self,
        search_frame,
        location: Tuple[int, int],
        template_shape: Tuple[int, int],
        guess: int,
        confidence: float,
    ) -> None:
        """Saves real Projekt Hard bubble crops while passive debug mode is active."""
        try:
            x, y = location
            t_h, t_w = template_shape
            pad = 28
            y1 = max(0, y - pad)
            y2 = min(search_frame.shape[0], y + t_h + pad)
            x1 = max(0, x - pad)
            x2 = min(search_frame.shape[1], x + t_w + pad)
            crop = search_frame[y1:y2, x1:x2]
            if crop.size == 0:
                return

            samples_dir = os.path.join(get_resource_path("assets"), "projekt_hard_samples")
            os.makedirs(samples_dir, exist_ok=True)

            timestamp = time.strftime("%Y%m%d_%H%M%S")
            millis = int((time.time() % 1) * 1000)
            filename = f"ph_bubble_{timestamp}_{millis:03d}_guess{guess}_conf{confidence:.2f}.png"
            path = os.path.join(samples_dir, filename)
            latest_path = os.path.join(samples_dir, "latest_bubble.png")

            cv2.imwrite(path, crop)
            cv2.imwrite(latest_path, crop)
            self._ph_sample_count += 1

            now = time.time()
            if self.on_status_update and now - self._last_ph_sample_log >= 2.0:
                self._last_ph_sample_log = now
                self.on_status_update(
                    f"[W{self.bot_id+1}] PH sample saved #{self._ph_sample_count}: "
                    f"{filename}"
                )
        except Exception as e:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] PH sample save error: {e}")

    def _save_projekt_hard_miss_sample(self, reason: str) -> None:
        """Saves current PH search/full frames when OCR says a bubble was missed."""
        try:
            frame = self.capture_full_window()
            h, w = frame.shape[:2]
            crop_left = max(0, int(w * 0.02))
            crop_right = min(w, int(w * 0.78))
            crop_top = max(0, int(h * 0.16))
            crop_bottom = min(h, int(h * 0.88))
            search = frame[crop_top:crop_bottom, crop_left:crop_right]

            misses_dir = os.path.join(get_resource_path("assets"), "projekt_hard_misses")
            os.makedirs(misses_dir, exist_ok=True)
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            millis = int((time.time() % 1) * 1000)
            base = f"ph_miss_{timestamp}_{millis:03d}_{reason}"
            full_path = os.path.join(misses_dir, f"{base}_full.png")
            roi_path = os.path.join(misses_dir, f"{base}_roi.png")
            cv2.imwrite(full_path, frame)
            cv2.imwrite(roi_path, search)
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Guarde miss PH: {os.path.basename(roi_path)}")
        except Exception as e:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Error guardando miss PH: {e}")

    def _save_projekt_hard_detection_crop(
        self,
        search_frame,
        location: Tuple[int, int],
        template_shape: Tuple[int, int],
        guess: int,
        number_confidence: float,
        color_confidence: float,
        scores: Dict[int, float],
    ) -> None:
        """Saves exact accepted bubble crops for manual verification."""
        try:
            x, y = location
            t_h, t_w = template_shape
            pad = 18
            y1 = max(0, y - pad)
            y2 = min(search_frame.shape[0], y + t_h + pad)
            x1 = max(0, x - pad)
            x2 = min(search_frame.shape[1], x + t_w + pad)
            crop = search_frame[y1:y2, x1:x2]
            if crop.size == 0:
                return

            detections_dir = os.path.join(get_resource_path("assets"), "projekt_hard_detections")
            os.makedirs(detections_dir, exist_ok=True)
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            millis = int((time.time() % 1) * 1000)
            score_text = "_".join(f"{count}-{scores.get(count, 0.0):.2f}" for count in (1, 2, 3))
            filename = (
                f"ph_detect_{timestamp}_{millis:03d}_guess{guess}_"
                f"num{number_confidence:.2f}_color{color_confidence:.2f}_scores{score_text}.png"
            )
            path = os.path.join(detections_dir, filename)
            latest_path = os.path.join(detections_dir, "latest_detection.png")
            cv2.imwrite(path, crop)
            cv2.imwrite(latest_path, crop)
            self._ph_detection_sample_count += 1
            if self.on_status_update:
                self.on_status_update(
                    f"[W{self.bot_id+1}] Crop globo guardado #{self._ph_detection_sample_count}: {filename}"
                )
        except Exception as e:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Error guardando crop detectado PH: {e}")

    def _save_projekt_hard_recent_miss_frames(self, reason: str) -> None:
        """Saves buffered PH wait frames so missed bubbles can be inspected."""
        try:
            if not self._ph_recent_frames:
                self._save_projekt_hard_miss_sample(reason)
                return

            misses_dir = os.path.join(get_resource_path("assets"), "projekt_hard_misses")
            os.makedirs(misses_dir, exist_ok=True)
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            saved = 0
            frames = list(self._ph_recent_frames)[-10:]
            for idx, (frame_time, frame, search) in enumerate(frames):
                age_ms = int((time.time() - frame_time) * 1000)
                base = f"ph_missbuf_{timestamp}_{reason}_{idx:02d}_{age_ms}ms"
                cv2.imwrite(os.path.join(misses_dir, f"{base}_full.png"), frame)
                cv2.imwrite(os.path.join(misses_dir, f"{base}_roi.png"), search)
                saved += 1
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Guarde buffer miss PH: {saved} frames ({reason})")
        except Exception as e:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Error guardando buffer PH: {e}")

    def wait_for_projekt_hard_bubble(self, timeout: float = 40.0, baseline_text: str = "") -> Tuple[int, float]:
        """Waits for the Projekt Hard bubble and returns required space presses."""
        if self.on_status_update:
            self.on_status_update(f"[W{self.bot_id+1}] Detectando globo PH por hasta {timeout:.1f}s...")
        count, confidence = self.observe_projekt_hard_bubble(timeout=timeout, baseline_text=baseline_text)
        if count:
            if self.on_status_update:
                self.on_status_update(
                    f"[W{self.bot_id+1}] Enviando {count} barra(s) por globo={count} "
                    f"(conf {confidence:.2f})"
                )
            return (count, confidence)

        if self.on_status_update:
            if self._ph_wait_abort_state:
                self.on_status_update(
                    f"[W{self.bot_id+1}] Corto espera de globo por OCR={self._ph_wait_abort_state} "
                    f"(best conf {confidence:.2f})"
                )
            else:
                self.on_status_update(f"[W{self.bot_id+1}] Projekt Hard bubble timeout/inconclusive (best conf {confidence:.2f})")
        return (0, confidence)

    def wait_for_projekt_hard_reel_result(self, press_count: int) -> bool:
        """Waits until Projekt Hard leaves the bubble/reel state before re-baiting."""
        start_time = time.time()
        resend_done = False
        visible_frames = 0

        time.sleep(0.25)
        while self.running and time.time() - start_time < 7.0:
            if self.paused:
                time.sleep(0.1)
                start_time += 0.1
                continue

            self.detect_projekt_hard_bubble()
            visible_source = self._last_ph_visible_source
            template_still_is_confident = self._projekt_hard_template_choice_is_confident(
                self._last_ph_best_count,
                self._last_ph_best_confidence,
                dict(self._last_ph_scores),
            )

            if visible_source == "none" or (visible_source == "template" and not template_still_is_confident):
                if self.on_status_update:
                    self.on_status_update("[W{}] PH reel confirmado: globo/espera finalizo".format(self.bot_id + 1))
                return True

            visible_frames += 1
            elapsed = time.time() - start_time
            if not resend_done and elapsed >= 0.90 and visible_frames >= 4:
                resend_done = True
                if self.on_status_update:
                    self.on_status_update(
                        f"[W{self.bot_id+1}] PH reel sigue activo; reenviando "
                        f"{press_count} barra(s) una vez"
                    )
                for idx in range(press_count):
                    if not self.running:
                        break
                    while self.paused and self.running:
                        time.sleep(0.1)
                    self.press_projekt_space(idx + 1, press_count)
                    if idx + 1 < press_count:
                        self.wait_between_projekt_spaces()

            time.sleep(0.08)

        if self.on_status_update:
            self.on_status_update(
                "[W{}] PH reel no confirmo salida; espero antes de reintentar cebo".format(self.bot_id + 1)
            )
        return False

    def _projekt_ocr_available(self) -> bool:
        return StorageFile is not None and BitmapDecoder is not None and OcrEngine is not None and Language is not None

    def _get_projekt_ocr_engine(self):
        if not self._projekt_ocr_available():
            return None
        if self._ph_ocr_engine is not None:
            return self._ph_ocr_engine
        try:
            engine = OcrEngine.try_create_from_language(Language("es-ES"))
            if engine is None:
                engine = OcrEngine.try_create_from_user_profile_languages()
            self._ph_ocr_engine = engine
            return engine
        except Exception as e:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] OCR no disponible: {e}")
            return None

    def open_projekt_chat(self):
        """Opens the Projekt Hard message log with L when OCR cannot already read it."""
        try:
            if self._projekt_ocr_available():
                current_text = self.read_projekt_chat_text()
                if self.projekt_message_log_is_visible(current_text):
                    self.remember_current_projekt_private_lines(current_text)
                    if self.on_status_update:
                        self.on_status_update(f"[W{self.bot_id+1}] Registro de mensajes ya visible para OCR")
                    return
            for attempt in range(1, 4):
                self.press_key('l', f"Registro de mensajes abierto (L) intento {attempt}/3")
                time.sleep(0.35)
                if not self._projekt_ocr_available():
                    return
                current_text = self.read_projekt_chat_text()
                if self.projekt_message_log_is_visible(current_text):
                    self.remember_current_projekt_private_lines(current_text)
                    if self.on_status_update:
                        self.on_status_update(f"[W{self.bot_id+1}] Registro de mensajes confirmado por OCR")
                    return
        except Exception:
            pass

    def capture_projekt_chat_area(self):
        """Captures the visible Projekt Hard chat/message-log area."""
        frame = self.capture_full_window()
        h, w = frame.shape[:2]
        # The opened message log can occupy most of the left side of the client.
        # The state we need is the lowest visible <Pesca> line, so keep nearly
        # the full log height and exclude the right inventory/input bar.
        top = 0
        bottom = min(h, int(h * 0.955))
        left = 0
        right = min(w, int(w * 0.84))
        return frame[top:bottom, left:right]

    def _prepare_chat_ocr_image(self, chat_frame):
        gray = cv2.cvtColor(chat_frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, None, fx=2.5, fy=2.5, interpolation=cv2.INTER_CUBIC)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        mask = gray > 105
        prepared = np.full_like(gray, 255)
        prepared[mask] = 0
        return prepared

    async def _read_projekt_chat_text_async(self, image_path: str) -> str:
        engine = self._get_projekt_ocr_engine()
        if engine is None:
            return ""
        file = await StorageFile.get_file_from_path_async(image_path)
        stream = await file.open_read_async()
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        result = await engine.recognize_async(bitmap)
        return result.text or ""

    def read_projekt_chat_text(self) -> str:
        """Reads visible chat text using Windows OCR."""
        if not self._projekt_ocr_available():
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] OCR no instalado/disponible")
            return ""
        try:
            chat_frame = self.capture_projekt_chat_area()
            prepared = self._prepare_chat_ocr_image(chat_frame)
            path = os.path.abspath(get_resource_path("projekt_hard_chat_ocr.png"))
            cv2.imwrite(path, prepared)
            text = asyncio.run(self._read_projekt_chat_text_async(path))
            lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
            text = "\n".join(line for line in lines if line)
            self._ph_last_chat_text = text
            return text
        except Exception as e:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] OCR chat error: {e}")
            return ""

    def get_last_projekt_fishing_line(self, text: str) -> str:
        """Returns the last visible <Pesca> message from OCR text."""
        if not text:
            return ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        normalized = (
            text.replace(chr(0x00AB), "<")
            .replace(chr(0x00BB), ">")
            .replace("Â«", "<")
            .replace("Â»", ">")
        )
        normalized = (
            normalized.replace("{Pesca>", "<Pesca>")
            .replace("[Pesca>", "<Pesca>")
            .replace("|Pesca>", "<Pesca>")
        )
        lines = [line.strip() for line in normalized.splitlines() if line.strip()]
        matches = re.findall(r"<?\s*pesca\s*>?.*?(?=<?\s*pesca\s*>?|$)", normalized, flags=re.IGNORECASE)
        matches = [match.strip() for match in matches if match.strip()]
        if matches:
            return re.sub(r"\s+normal\s*$", "", matches[-1], flags=re.IGNORECASE).strip()

        fishing_lines = [line for line in lines if "pesca" in line.lower()]
        if fishing_lines:
            return fishing_lines[-1]
        return lines[-1] if lines else ""

    def projekt_message_log_is_visible(self, text: str) -> bool:
        """Returns True when OCR sees the expanded Projekt Hard message log."""
        if not text:
            return False
        normalized = unicodedata.normalize("NFD", text.lower())
        normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
        if "registro de mensajes" in normalized:
            return True
        pesca_count = len(re.findall(r"<?\s*pesca\s*>?", normalized, flags=re.IGNORECASE))
        return pesca_count >= 4

    def normalize_projekt_chat_for_detection(self, text: str) -> str:
        normalized = unicodedata.normalize("NFD", text.lower())
        normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
        replacements = {
            "Ã¡": "a", "Ã©": "e", "Ã­": "i", "Ã³": "o", "Ãº": "u",
            "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u",
            "«": "<", "»": ">", "|": "i",
        }
        for old, new in replacements.items():
            normalized = normalized.replace(old, new)
        return re.sub(r"\s+", " ", normalized).strip()

    def find_projekt_private_message_lines(self, text: str) -> list:
        """Returns OCR lines that look like private/whisper messages."""
        if not text:
            return []

        private_lines = []
        for raw_line in text.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line:
                continue
            normalized = self.normalize_projekt_chat_for_detection(line)
            if not normalized:
                continue

            # Ignore stable UI labels and normal fishing/system messages.
            if "pesca" in normalized or "registro de mensajes" in normalized:
                continue
            if normalized in ("todo normal grupo gremio gritar informacion anuncios", "normal"):
                continue

            private_signal = (
                "susurr" in normalized
                or "susurro" in normalized
                or "susurros" in normalized
                or "whisper" in normalized
                or "mensaje privado" in normalized
                or "<privado>" in normalized
                or "[privado]" in normalized
                or "<mp>" in normalized
                or "[mp]" in normalized
                or "<pm>" in normalized
                or "[pm]" in normalized
            )
            if private_signal:
                private_lines.append(line)

        return private_lines

    def remember_current_projekt_private_lines(self, text: str):
        """Marks existing visible private lines as already handled."""
        for line in self.find_projekt_private_message_lines(text):
            key = self.normalize_projekt_chat_for_detection(line)
            if key:
                self._ph_seen_private_lines.add(key)

    def check_projekt_private_messages(self, text: str, baseline_text: str = "") -> bool:
        """Pauses this bot if a new visible private message appears in chat OCR."""
        if not self.config.get('projekt_private_pause_enabled', True):
            return False
        if not text:
            return False

        baseline_keys = {
            self.normalize_projekt_chat_for_detection(line)
            for line in self.find_projekt_private_message_lines(baseline_text)
        }
        for line in self.find_projekt_private_message_lines(text):
            key = self.normalize_projekt_chat_for_detection(line)
            if not key or key in baseline_keys or key in self._ph_seen_private_lines:
                continue

            self._ph_seen_private_lines.add(key)
            self._ph_private_pause_triggered = True
            self.paused = True
            if self.on_status_update:
                preview = line[-160:] if len(line) > 160 else line
                self.on_status_update(
                    f"[W{self.bot_id+1}] PRIVADO detectado por OCR; bot pausado: {preview}"
                )
            try:
                winsound.Beep(1200, 250)
                winsound.Beep(900, 250)
            except Exception:
                pass
            return True
        return False

    def classify_projekt_chat_text(self, text: str) -> str:
        last_line = self.get_last_projekt_fishing_line(text)
        normalized = (last_line or text).lower()
        normalized = unicodedata.normalize("NFD", normalized)
        normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
        normalized = normalized.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
        normalized = normalized.replace("seleccionado", "seleccionado")

        if (
            "numero incorrecto" in normalized
            or "incorrecto de puls" in normalized
            or "hcorrecto" in normalized
            or "ocorrecto" in normalized
            or "correcto de puls" in normalized
            or "pulsaciones de espacio" in normalized
        ):
            return "wrong_presses"
        if "no tienes cebo" in normalized:
            return "no_bait"
        if "demasiado tiempo" in normalized or "espera para un pez" in normalized:
            return "too_late"
        if "gusano" in normalized and (
            "cebo" in normalized
            or "como" in normalized
            or "cmno" in normalized
            or "sustitu" in normalized
            or "amado" in normalized
        ):
            return "bait_selected"
        if "pescaste" in normalized:
            return "caught"
        if "escapo" in normalized or "mala suerte" in normalized:
            return "escaped"
        if "nivel de cana" in normalized or "nivel de ca" in normalized:
            return "rod_level"
        if "permiso" in normalized or "no tienes permitido" in normalized:
            return "no_permit"
        return "unknown"

    def wait_for_projekt_chat_result(self, timeout: float = 8.0, baseline_text: str = "") -> Tuple[str, str]:
        """Waits for a terminal fishing chat message after reel spaces."""
        start_time = time.time()
        last_text = baseline_text or ""
        while self.running and time.time() - start_time < timeout:
            if self.paused:
                time.sleep(0.1)
                start_time += 0.1
                continue
            text = self.read_projekt_chat_text()
            if text and text != last_text:
                if self.check_projekt_private_messages(text, baseline_text=baseline_text):
                    return ("private_message", text)
                last_text = text
                state = self.classify_projekt_chat_text(text)
                if self.on_status_update:
                    last_line = self.get_last_projekt_fishing_line(text)
                    preview = last_line or text
                    preview = preview[-140:] if len(preview) > 140 else preview
                    self.on_status_update(f"[W{self.bot_id+1}] OCR ultima linea: {state} | {preview}")
                if state in ("caught", "escaped", "too_late", "wrong_presses", "rod_level", "no_bait", "no_permit"):
                    self._ph_last_chat_result = state
                    return (state, text)
            time.sleep(0.18)
        return ("timeout", last_text)

    def wait_for_projekt_bait_selected(self, timeout: float = 2.5, baseline_text: str = "") -> bool:
        """Optionally confirms bait selection in the open chat."""
        start_time = time.time()
        last_text = baseline_text or ""
        while self.running and time.time() - start_time < timeout:
            if self.paused:
                time.sleep(0.1)
                start_time += 0.1
                continue
            text = self.read_projekt_chat_text()
            if text and text == last_text:
                time.sleep(0.25)
                continue
            if text:
                if self.check_projekt_private_messages(text, baseline_text=baseline_text):
                    return False
                last_text = text
            state = self.classify_projekt_chat_text(text)
            if state == "bait_selected":
                if self.on_status_update:
                    self.on_status_update(f"[W{self.bot_id+1}] OCR confirmo cebo seleccionado")
                return True
            if state in ("no_bait", "too_late", "wrong_presses"):
                return False
            time.sleep(0.25)
        return False
    
    def wait_for_classic_fish(self, timeout: float = 10.0) -> bool:
        """Waits for the classic fish image to appear in the game window.
        Returns True if found, False if timeout."""
        template = self._load_classic_fish_template()
        if template is None:
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] No classic fish template, using fallback timing")
            return True  # Fallback: proceed anyway
        
        start_time = time.time()
        t_h, t_w = template.shape

        # Pre-compute all scaled variants once — reused on every poll iteration
        _scales_raw = [0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 2.25, 2.5, 2.75, 3.0]
        scaled_templates = []
        for _s in _scales_raw:
            _nw, _nh = int(t_w * _s), int(t_h * _s)
            if _nw >= 10 and _nh >= 10:
                _interp = cv2.INTER_AREA if _s < 1 else cv2.INTER_LINEAR
                scaled_templates.append((_s, _nw, _nh, cv2.resize(template, (_nw, _nh), interpolation=_interp)))

        while self.running and time.time() - start_time < timeout:
            if self.paused:
                time.sleep(0.1)
                continue

            try:
                frame = self.capture_full_window()
                frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

                # Crop to 250px wide centered bar, upper half only for performance
                f_h, f_w = frame_gray.shape
                center_x = f_w // 2
                crop_left = max(0, center_x - 125)
                crop_right = min(f_w, center_x + 125)
                crop_bottom = f_h // 2  # Only upper half
                frame_gray = frame_gray[:crop_bottom, crop_left:crop_right]

                f_h, f_w = frame_gray.shape

                # Multi-scale template matching (uses pre-computed scaled templates)
                best_match_val = 0
                best_scale = 1.0

                for scale, new_w, new_h, scaled_template in scaled_templates:
                    # Skip if scaled template is larger than current cropped frame
                    if new_h > f_h or new_w > f_w:
                        continue

                    # Template matching
                    result = cv2.matchTemplate(frame_gray, scaled_template, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, _ = cv2.minMaxLoc(result)

                    if max_val > best_match_val:
                        best_match_val = max_val
                        best_scale = scale

                    # Early exit if we found a very good match
                    if max_val >= 0.8:
                        break
                
                if best_match_val >= 0.7:  # Found the classic fish indicator
                    # Start timer IMMEDIATELY after detection (configurable delay)
                    delay = self.config.get('classic_fishing_delay', 3.0)
                    # Use interruptible sleep that checks running/paused state
                    delay_start = time.time()
                    while time.time() - delay_start < (delay - 0.05):
                        if not self.running:
                            return False  # Bot stopped during delay
                        if self.paused:
                            time.sleep(0.1)
                            delay_start = time.time()  # Reset delay when paused
                            continue
                        time.sleep(0.05)  # Small sleep increments
                    if self.on_status_update:
                        self.on_status_update(f"[W{self.bot_id+1}] Classic fish detected (confidence: {best_match_val:.2f}, scale: {best_scale:.1f}x, delay: {delay}s)")
                    return True
                
                time.sleep(0.02)  # Fast polling
            except Exception as e:
                if self.on_status_update:
                    self.on_status_update(f"[W{self.bot_id+1}] Error detecting classic fish: {e}")
                time.sleep(0.1)
        
        if self.on_status_update:
            self.on_status_update(f"[W{self.bot_id+1}] Classic fish detection timeout")
        return False
    
    def play_game(self):
        """Main game loop implementing the fishing minigame workflow."""
        # Refresh timing cache from config once per session — never read inside loops
        self._t_cursor   = self.config.get('timing_cursor_settle',  0.012)
        self._t_hold     = self.config.get('timing_button_hold',    0.008)
        self._t_post     = self.config.get('timing_post_click',     0.035)
        self._t_human_mn = self.config.get('timing_human_min',      0.15)
        self._t_human_mx = self.config.get('timing_human_max',      0.40)
        self._t_key_hold = self.config.get('timing_key_hold',       0.025)
        self._t_key_set  = self.config.get('timing_key_settle',     0.030)
        self._t_interkey = self.config.get('timing_cast_interkey',  0.350)
        self._t_catch_wait  = self.config.get('timing_catch_wait',        0.400)
        self._t_open_wait   = self.config.get('timing_open_wait',         0.100)
        self._t_dead_check  = self.config.get('timing_dead_fish_check',   0.100)
        self._t_drop_settle = self.config.get('timing_drop_settle',       0.120)
        self._t_qs_between  = self.config.get('timing_quickskip_between', 0.100)
        self._t_qs_after    = self.config.get('timing_quickskip_after',   0.100)
        self._t_ph_bubble_timeout = max(30.000, self.config.get('timing_projekt_bubble_timeout', 35.000))
        self._t_ph_retry_wait     = self.config.get('timing_projekt_retry_wait',     0.700)
        self._t_ph_space_gap      = self.config.get('timing_projekt_space_gap',      0.300)
        self._t_ph_bait_to_cast_min = self.config.get('timing_projekt_bait_to_cast_min', 0.152)
        self._t_ph_bait_to_cast_max = self.config.get('timing_projekt_bait_to_cast_max', 0.746)
        self._t_ph_space_gap_min = self.config.get('timing_projekt_space_gap_min', 0.300)
        self._t_ph_space_gap_max = self.config.get('timing_projekt_space_gap_max', 0.800)
        self._t_ph_after_result_min = self.config.get('timing_projekt_after_result_min', 1.000)
        self._t_ph_after_result_max = self.config.get('timing_projekt_after_result_max', 5.000)
        self._t_ph_post_reel_wait = self.config.get('timing_projekt_post_reel_wait', 5.000)
        self._t_ph_pre_reel_wait  = self.config.get('timing_projekt_pre_reel_wait',  1.500)
        self._t_ph_space_hold     = self.config.get('timing_projekt_space_hold',     0.080)

        # Reset bait if starting with 0 or negative bait
        max_bait = len(self.bait_keys) * 200
        if self.bait_counter <= 0:
            self.bait_counter = max_bait
            if self.on_bait_update:
                self.on_bait_update(self.bot_id, self.bait_counter)
            if self.on_status_update:
                self.on_status_update(f"[W{self.bot_id+1}] Bait counter was 0! Reset to {max_bait}.")
        
        if self.on_status_update:
            self.on_status_update(f"[W{self.bot_id+1}] Bot started! Bait: {self.bait_counter}")

        if self.config.get('projekt_hard_fishing', False):
            self.reset_projekt_camera()
        
        # Scan all inventory pages at startup, process open/drop items, then settle on
        # the first page that still has empty slots.
        self._startup_scan_and_process_all_pages()

        _was_paused = False

        while self.running and self.bait_counter > 0:
            if self.paused:
                _was_paused = True
                time.sleep(0.1)
                continue

            if _was_paused:
                _was_paused = False
                if self.on_status_update:
                    self.on_status_update(f"[W{self.bot_id+1}] Resumed — re-scanning inventory...")
                self._startup_scan_and_process_all_pages()
                if not self.running:
                    break

            try:
                if self.config.get('projekt_hard_fishing', False) and not self.has_projekt_hard_fishing_permit():
                    if self.on_status_update:
                        self.on_status_update(f"[W{self.bot_id+1}] Projekt Hard fishing permit not detected. Stopping bot.")
                    self.running = False
                    if self.on_bot_stop:
                        self.on_bot_stop(self.bot_id)
                    break

                if self.config.get('projekt_hard_fishing', False):
                    if self._projekt_ocr_available() and not self._ph_chat_open_attempted:
                        self._ph_chat_open_attempted = True
                        self.open_projekt_chat()
                        if self.on_status_update:
                            self.on_status_update(f"[W{self.bot_id+1}] OCR chat activo; usando L para registro de mensajes")

                    if self.config.get('projekt_hard_debug_only', False):
                        if self.on_status_update:
                            self.on_status_update(
                                f"[W{self.bot_id+1}] Projekt Hard detect-only mode active. "
                                "Fish manually; bot will log scores and save bubble samples."
                            )
                        while self.running:
                            if self.paused:
                                time.sleep(0.1)
                                continue
                            count, confidence = self.observe_projekt_hard_bubble(
                                timeout=1.25,
                                use_ocr_abort=False,
                                perform_reel_delay=False,
                            )
                            if not count:
                                time.sleep(0.1)
                        break

                    max_attempts = 3
                    press_count = 0
                    confidence = 0.0

                    for attempt in range(1, max_attempts + 1):
                        if not self.running:
                            break
                        while self.paused and self.running:
                            time.sleep(0.1)
                        if not self.running:
                            break

                        chat_before_cast = self.projekt_bait_and_cast()
                        if chat_before_cast == "__bait_failed__":
                            time.sleep(self._t_ph_retry_wait)
                            continue

                        chat_before_reel = chat_before_cast
                        if self._projekt_ocr_available():
                            if self.on_status_update:
                                self.on_status_update("[W{}] OCR post-lanzamiento: reviso ultima linea antes de esperar globo".format(self.bot_id + 1))
                            cast_state, cast_text = self.wait_for_projekt_chat_result(timeout=0.6, baseline_text=chat_before_cast)
                            if cast_text:
                                chat_before_reel = cast_text
                            if cast_state == "private_message":
                                continue
                            if cast_state in ("no_bait", "too_late", "wrong_presses"):
                                if self.on_status_update:
                                    self.on_status_update(
                                        f"[W{self.bot_id+1}] Chat bloqueo lanzamiento ({cast_state}); "
                                        "reintento cebo/lanzar sin esperar globo"
                                    )
                                if cast_state in ("too_late", "wrong_presses"):
                                    self.wait_after_projekt_terminal_result()
                                else:
                                    time.sleep(self._t_ph_retry_wait)
                                continue
                            if cast_state == "no_permit":
                                if self.on_status_update:
                                    self.on_status_update(f"[W{self.bot_id+1}] Chat indica falta de permiso; detengo PH")
                                self.running = False
                                break

                        press_count, confidence = self.wait_for_projekt_hard_bubble(
                            timeout=self._t_ph_bubble_timeout,
                            baseline_text=chat_before_reel,
                        )
                        if press_count > 0 or not self.running:
                            break

                        if self._ph_wait_abort_state == "private_message":
                            continue

                        if self._ph_wait_abort_state in ("too_late", "wrong_presses", "no_bait"):
                            if self.on_status_update:
                                self.on_status_update(
                                    f"[W{self.bot_id+1}] Reintento inmediato por OCR={self._ph_wait_abort_state}"
                                )
                            time.sleep(self._t_ph_retry_wait)
                            continue

                        while self.paused and self.running:
                            time.sleep(0.1)
                        if not self.running:
                            break

                        if self.on_status_update:
                            self.on_status_update(
                                f"[W{self.bot_id+1}] No Projekt Hard bubble after cast "
                                f"(attempt {attempt}/{max_attempts}, best conf {confidence:.2f}). Re-baiting..."
                            )
                        time.sleep(self._t_ph_retry_wait)

                    if not self.running:
                        break

                    if press_count <= 0:
                        self.consecutive_failures += 1
                        if self.on_status_update:
                            self.on_status_update(f"[W{self.bot_id+1}] Projekt Hard cast failed after retries ({self.consecutive_failures}/2)")

                        if self.consecutive_failures >= 2:
                            self.adjust_bait_tier()
                            if self.bait_counter <= 0:
                                if self.on_status_update:
                                    self.on_status_update(f"[W{self.bot_id+1}] Bait depleted after Projekt Hard failures. Stopping bot.")
                                self.running = False
                                if self.on_bot_stop:
                                    self.on_bot_stop(self.bot_id)
                                break
                        continue

                    self.consecutive_failures = 0

                    for idx in range(press_count):
                        if not self.running:
                            break
                        while self.paused and self.running:
                            _was_paused = True
                            time.sleep(0.1)
                        if not self.running:
                            break
                        self.press_projekt_space(idx + 1, press_count)
                        if idx + 1 < press_count:
                            self.wait_between_projekt_spaces()

                    chat_state = "ocr_disabled"
                    if self._projekt_ocr_available():
                        if self.on_status_update:
                            self.on_status_update("[W{}] OCR resultado: espero nueva ultima linea de pesca".format(self.bot_id + 1))
                        chat_state, chat_text = self.wait_for_projekt_chat_result(
                            timeout=max(8.0, self._t_ph_post_reel_wait + 2.0),
                            baseline_text=chat_before_reel,
                        )
                        if chat_state == "timeout":
                            if self.on_status_update:
                                self.on_status_update("[W{}] OCR no confirmo resultado nuevo; uso confirmacion visual".format(self.bot_id + 1))
                            self.wait_for_projekt_hard_reel_result(press_count)
                        else:
                            if self.on_status_update:
                                self.on_status_update(
                                    f"[W{self.bot_id+1}] Resultado chat PH: {chat_state}; "
                                    f"ya envie {press_count} barra(s)"
                                )
                            if chat_state == "private_message":
                                continue
                            if chat_state in ("too_late", "wrong_presses", "no_bait"):
                                if chat_state in ("too_late", "wrong_presses"):
                                    self.wait_after_projekt_terminal_result()
                                else:
                                    time.sleep(0.15)
                            elif chat_state in ("caught", "escaped", "rod_level"):
                                self.wait_after_projekt_terminal_result()
                            else:
                                time.sleep(0.8)
                    else:
                        self.wait_for_projekt_hard_reel_result(press_count)
                        if self.on_status_update:
                            self.on_status_update(
                                f"[W{self.bot_id+1}] Esperando {self._t_ph_post_reel_wait:.1f}s "
                                "antes de volver a cebar/lanzar"
                            )
                        self.handle_caught_item()

                    self.total_games += 1
                    if chat_state != "no_bait":
                        self.bait_counter -= 1

                    if self.on_status_update:
                        self.on_status_update(
                            f"[W{self.bot_id+1}] Pesca PH cerrada ({press_count} barra(s)). "
                            f"Total: {self.total_games}, Cebo: {self.bait_counter}"
                        )
                    if self.on_bait_update:
                        self.on_bait_update(self.bot_id, self.bait_counter)
                    if self.on_stats_update:
                        self.on_stats_update(self.bot_id, 0, self.total_games, self.bait_counter)

                    time.sleep(0.1)
                    continue

                self.bait_and_cast()
                
                # Only play minigame if Classic Fishing system is NOT enabled
                if not self.config.get('classic_fishing', False):
                    minigame_detected = self.wait_for_minigame_window(timeout=6)
                    if not minigame_detected:
                        self.consecutive_failures += 1
                        if self.on_status_update:
                            self.on_status_update(f"[W{self.bot_id+1}] Minigame not detected ({self.consecutive_failures}/5)")
                        
                        if self.consecutive_failures >= 5:
                            self.adjust_bait_tier()
                            if self.bait_counter <= 0:
                                if self.on_status_update:
                                    self.on_status_update(f"[W{self.bot_id+1}] Bait depleted after consecutive failures. Stopping bot.")
                                self.running = False
                                if self.on_bot_stop:
                                    self.on_bot_stop(self.bot_id)
                                break
                        
                        continue
                    
                    # Reset failure counter on successful minigame detection
                    self.consecutive_failures = 0
                    
                    minigame_active = True
                    human_like = self.config.get('human_like_clicking', True)
                    
                    while self.running and minigame_active:
                        if self.paused:
                            _was_paused = True
                            time.sleep(0.1)
                            continue

                        # Small delay between attempts (minimized for responsiveness)
                        if human_like:
                            time.sleep(np.random.uniform(self._t_human_mn, self._t_human_mx))
                        
                        try:
                            # Atomic operation: capture + detect + click all within lock
                            window_active, fish_pos = self.atomic_capture_and_click()
                            
                            if not window_active:
                                # Minigame ended
                                minigame_active = False
                                self.total_games += 1
                                self.bait_counter -= 1
                                
                                if self.on_status_update:
                                    self.on_status_update(f"[W{self.bot_id+1}] Game finished. Total: {self.total_games}, Bait: {self.bait_counter}")
                                if self.on_bait_update:
                                    self.on_bait_update(self.bot_id, self.bait_counter)
                                if self.on_stats_update:
                                    self.on_stats_update(self.bot_id, 0, self.total_games, self.bait_counter)
                                
                                # Handle caught item (if auto fish handling is enabled)
                                self.handle_caught_item()
                                break
                            
                            if fish_pos:
                                self.hits += 1
                                if self.on_stats_update:
                                    self.on_stats_update(self.bot_id, self.hits, self.total_games, self.bait_counter)
                                
                        except Exception as e:
                            if self.on_status_update:
                                self.on_status_update(f"[W{self.bot_id+1}] Error: {e}")
                    
                    self.hits = 0
                    if self.bait_counter > 0:
                        if self.config.get('quick_skip', False):
                            self.quickskip()
                        else:
                            # Interruptible wait that respects pause state
                            wait_time = np.random.uniform(4, 4.5)
                            wait_end = time.time() + wait_time
                            while time.time() < wait_end and self.running:
                                if self.paused:
                                    _was_paused = True
                                    time.sleep(0.1)
                                    continue
                                time.sleep(0.05)
                else:
                    # Classic Fishing system - wait for fish indicator, then reel in
                    # Step 1: Wait for classic fish image to appear
                    fish_found = self.wait_for_classic_fish(timeout=40)
                    
                    # Check if bot was stopped during wait
                    if not self.running:
                        break
                    
                    if not fish_found:
                        # Timeout waiting for fish - handle consecutive failures
                        self.consecutive_failures += 1
                        if self.on_status_update:
                            self.on_status_update(f"[W{self.bot_id+1}] No fish bite detected ({self.consecutive_failures}/2), recasting...")
                        
                        if self.consecutive_failures >= 2:
                            self.adjust_bait_tier()
                            if self.bait_counter <= 0:
                                if self.on_status_update:
                                    self.on_status_update(f"[W{self.bot_id+1}] Bait depleted after consecutive failures. Stopping bot.")
                                self.running = False
                                if self.on_bot_stop:
                                    self.on_bot_stop(self.bot_id)
                                break
                            
                        # Press CTRL+G once per failure to dismount horse if that's the issue
                        # First failure: try to dismount if on horse
                        # Second failure: you actually mounted in first attemp and now you need to unmount
                        if self.on_status_update:
                            self.on_status_update(f"[W{self.bot_id+1}] Pressing CTRL+G to dismount horse...")
                        self.press_ctrl_key('g')
                        time.sleep(0.15)
                        continue
                    
                    # Reset failure counter on successful fish detection
                    self.consecutive_failures = 0
                    
                    # Handle pause before reeling in
                    while self.paused and self.running:
                        _was_paused = True
                        time.sleep(0.1)
                    if not self.running:
                        break
                    
                    # Timer already elapsed in wait_for_classic_fish - press space to reel in
                    # Acquire lock and activate window BEFORE pressing space (critical timing)
                    self.press_key('space', "Reel in fish")
                    time.sleep(0.05)
                    
                    # Handle caught item (if auto fish handling is enabled)
                    self.handle_caught_item()
                    
                    if self.on_status_update:
                        self.on_status_update(f"[W{self.bot_id+1}] Reeling in fish")
                    
                    # Update counters
                    self.total_games += 1
                    self.bait_counter -= 1
                    
                    if self.on_status_update:
                        self.on_status_update(f"[W{self.bot_id+1}] Classic catch! Total: {self.total_games}, Bait: {self.bait_counter}")
                    if self.on_bait_update:
                        self.on_bait_update(self.bot_id, self.bait_counter)
                    if self.on_stats_update:
                        self.on_stats_update(self.bot_id, 0, self.total_games, self.bait_counter)
                    
                    # Check if bot stopped before waiting
                    if not self.running:
                        break
                    
                    # Step 4: Quick skip or wait before next cast (with interruptible waits)
                    if self.bait_counter > 0:
                        if self.config.get('quick_skip', False):
                            # Interruptible 1 second wait
                            wait_end = time.time() + 0.5
                            while time.time() < wait_end and self.running:
                                if self.paused:
                                    _was_paused = True
                                    time.sleep(0.1)
                                    continue
                                time.sleep(0.05)
                            if not self.running:
                                break
                            self.quickskip()
                        else:
                            # Interruptible random wait
                            wait_time = np.random.uniform(4, 4.5)
                            wait_end = time.time() + wait_time
                            while time.time() < wait_end and self.running:
                                if self.paused:
                                    _was_paused = True
                                    time.sleep(0.1)
                                    continue
                                time.sleep(0.05)
                
            except Exception as e:
                if self.on_status_update:
                    self.on_status_update(f"[W{self.bot_id+1}] Error in play_game: {e}")
                time.sleep(0.5)
        
        if self.on_status_update:
            self.on_status_update(f"[W{self.bot_id+1}] Bot finished! Total games: {self.total_games}")
        self.running = False
        if self.on_bot_stop:
            self.on_bot_stop(self.bot_id)
    
    def start(self):
        """Starts the bot"""
        self.running = True
        self.play_game()
    
    def stop(self):
        """Stops the bot"""
        self.running = False
        if self.on_status_update:
            self.on_status_update(f"[W{self.bot_id+1}] Bot stopped")


if __name__ == "__main__":
    try:
        from version import VERSION
        from updater import check_for_update

        if check_for_update(VERSION):
            sys.exit(0)
    except Exception:
        pass

    from bot_gui import BotGUI
    gui = BotGUI()
    gui.run()
