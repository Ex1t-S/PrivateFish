import argparse
import os
import time

import cv2
import mss
import numpy as np
import pygetwindow as gw


def find_window(title_part: str):
    title_part = title_part.lower()
    matches = []
    for win in gw.getAllWindows():
        try:
            if win.title and title_part in win.title.lower() and getattr(win, "visible", True):
                matches.append(win)
        except Exception:
            pass
    if not matches:
        raise RuntimeError(f"No visible window contains: {title_part!r}")
    matches.sort(key=lambda w: (w.width * w.height), reverse=True)
    return matches[0]


def clamp_rect(left, top, right, bottom, width, height):
    left = max(0, min(width - 1, int(left)))
    top = max(0, min(height - 1, int(top)))
    right = max(left + 1, min(width, int(right)))
    bottom = max(top + 1, min(height, int(bottom)))
    return left, top, right, bottom


def main():
    parser = argparse.ArgumentParser(description="Capture and annotate Projekt Hard window ROI.")
    parser.add_argument("--title", default="Projekt Hard")
    parser.add_argument("--out", default=os.path.join("assets", "projekt_hard_measure"))
    parser.add_argument("--top", type=float, default=0.38)
    parser.add_argument("--bottom", type=float, default=0.92)
    parser.add_argument("--left", type=float, default=0.00)
    parser.add_argument("--right", type=float, default=0.77)
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)

    win = find_window(args.title)
    if win.isMinimized:
        win.restore()
        time.sleep(0.2)

    left, top, width, height = win.left, win.top, win.width, win.height
    monitor = {"left": left, "top": top, "width": width, "height": height}

    with mss.mss() as sct:
        shot = np.array(sct.grab(monitor))
    frame = cv2.cvtColor(shot, cv2.COLOR_BGRA2BGR)

    roi_left, roi_top, roi_right, roi_bottom = clamp_rect(
        width * args.left,
        height * args.top,
        width * args.right,
        height * args.bottom,
        width,
        height,
    )

    annotated = frame.copy()
    cv2.rectangle(annotated, (roi_left, roi_top), (roi_right, roi_bottom), (0, 255, 255), 2)
    cv2.putText(
        annotated,
        f"ROI x={roi_left}:{roi_right} y={roi_top}:{roi_bottom}",
        (max(8, roi_left + 8), max(24, roi_top - 10)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (0, 255, 255),
        2,
        cv2.LINE_AA,
    )

    roi = frame[roi_top:roi_bottom, roi_left:roi_right]
    full_path = os.path.abspath(os.path.join(args.out, "projekt_hard_full.png"))
    roi_path = os.path.abspath(os.path.join(args.out, "projekt_hard_roi.png"))
    annotated_path = os.path.abspath(os.path.join(args.out, "projekt_hard_annotated.png"))

    cv2.imwrite(full_path, frame)
    cv2.imwrite(roi_path, roi)
    cv2.imwrite(annotated_path, annotated)

    print(f"Window title: {win.title}")
    print(f"Window rect absolute: left={left}, top={top}, width={width}, height={height}")
    print(f"ROI relative: x={roi_left}:{roi_right}, y={roi_top}:{roi_bottom}, width={roi_right-roi_left}, height={roi_bottom-roi_top}")
    print(f"Saved full: {full_path}")
    print(f"Saved ROI: {roi_path}")
    print(f"Saved annotated: {annotated_path}")


if __name__ == "__main__":
    main()
