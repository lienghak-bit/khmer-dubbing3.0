import http.server
import socketserver
import socket
import urllib.parse
import urllib.request
import asyncio
import os
import sys
import subprocess
import re
import shutil
import json
import threading
import uuid
import time
import traceback

# Global tasks status dictionary
active_tasks = {}
# Lock for thread-safe access to active_tasks entries
_tasks_lock = threading.Lock()

def update_task_progress(task_id, msg):
    with _tasks_lock:
        task = active_tasks.get(task_id)
        if not task:
            return
        if 'logs' not in task:
            task['logs'] = []
        task['logs'].append(msg)
        if len(task['logs']) > 80:
            task['logs'] = task['logs'][-80:]
        task['message'] = msg
        task['last_update'] = time.time()

        # Parse part or video progress: e.g. "📂 [វីដេអូទី 1/8]" or "📂 [ផ្នែកទី 2/4]"
        m_part = re.search(r"\[.*?\s*(\d+)/(\d+)\]", msg)
        if m_part:
            current_part = int(m_part.group(1))
            total_parts = int(m_part.group(2))
            task['current_part'] = current_part
            task['total_parts'] = total_parts
            calc_pct = int(((current_part - 1) / total_parts) * 85)
            task['progress_pct'] = max(task.get('progress_pct', 0), calc_pct)
            return

        # Parse TTS progress: "Generating TTS for subtitle 5/10..."
        m_sub = re.search(r"subtitle\s+(\d+)/(\d+)", msg, re.IGNORECASE)
        if m_sub:
            current_sub = int(m_sub.group(1))
            total_subs = int(m_sub.group(2))
            task['subtitles_done'] = current_sub
            task['total_subtitles'] = total_subs
            task['stage'] = 'tts'

            if 'current_part' in task and 'total_parts' in task:
                part = task['current_part']
                tot_parts = task['total_parts']
                part_start = ((part - 1) / tot_parts) * 85
                part_end = (part / tot_parts) * 85
                part_range = part_end - part_start
                sub_progress = (current_sub / total_subs) * part_range
                calc_pct = int(part_start + sub_progress)
            else:
                # TTS represents pre-processing audio (5% -> 25%), remaining 75% is video render
                calc_pct = int(5 + (current_sub / total_subs) * 20)
            task['progress_pct'] = max(task.get('progress_pct', 0), calc_pct)
            return

        if "Merging audio" in msg or "លាយសំឡេង" in msg or "Mix" in msg:
            task['progress_pct'] = max(task.get('progress_pct', 0), 25)
            task['stage'] = 'render'
        elif "រួមបញ្ចូលវីដេអូ" in msg or "concat" in msg.lower() or "តភ្ជាប់" in msg:
            task['progress_pct'] = max(task.get('progress_pct', 0), 93)
            task['stage'] = 'concat'
        elif "Cleaning" in msg or "សម្អាត" in msg:
            task['progress_pct'] = max(task.get('progress_pct', 0), 98)
            task['stage'] = 'finalize'
        elif "Saved permanent copy" in msg or "ជោគជ័យ" in msg or "ចម្លង" in msg:
            task['progress_pct'] = 100
            task['est_rem_sec'] = 0
            task['stage'] = 'completed'


def format_seconds_khmer(sec):
    sec = max(0, int(sec))
    m = sec // 60
    s = sec % 60
    if m > 0:
        return f"{m} នាទី {s} វិ" if s > 0 else f"{m} នាទី"
    return f"{s} វិ"


def run_ffmpeg_with_progress(cmd, total_dur_sec, log_callback, task_id=None, base_pct=25.0, max_pct=98.0, timeout=7200, stage_name="Render"):
    """
    Executes FFmpeg with real-time progress parsing (-progress pipe:1).
    Updates task['progress_pct'], task['est_rem_sec'], task['render_speed'], task['stage'] thread-safely.
    Ensures remaining time counts monotonically DOWN and never jumps up erratically.
    """
    cmd_with_prog = list(cmd[:-1]) + ['-progress', 'pipe:1', cmd[-1]]
    
    proc = subprocess.Popen(
        cmd_with_prog,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        bufsize=1
    )
    
    stderr_chunks = []
    def _read_stderr():
        try:
            for line in proc.stderr:
                stderr_chunks.append(line)
                if len(stderr_chunks) > 100:
                    stderr_chunks.pop(0)
        except Exception:
            pass

    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_err.start()

    cur_speed = 1.0
    last_log_t = 0.0
    
    try:
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if line.startswith('speed='):
                m = re.search(r'([\d\.]+)x', line)
                if m:
                    try:
                        cur_speed = max(0.1, float(m.group(1)))
                    except Exception:
                        pass
            elif line.startswith('out_time_us='):
                try:
                    us = int(line.split('=')[1])
                    cur_sec = us / 1_000_000.0
                    tot_sec = total_dur_sec if (total_dur_sec and total_dur_sec > 0) else cur_sec
                    ratio = min(1.0, max(0.0, cur_sec / tot_sec)) if tot_sec > 0 else 0.0
                    pct = base_pct + ratio * (max_pct - base_pct)
                    rem_vid_sec = max(0.0, tot_sec - cur_sec)
                    est_rem = max(1, round(rem_vid_sec / cur_speed)) if cur_speed > 0.05 else round(rem_vid_sec)
                    
                    if task_id:
                        with _tasks_lock:
                            task = active_tasks.get(task_id)
                            if task:
                                task['progress_pct'] = max(task.get('progress_pct', 0), round(pct, 1))
                                # Strictly monotonic countdown for estimated remaining seconds:
                                prev_est = task.get('est_rem_sec')
                                if prev_est is None or est_rem < prev_est:
                                    task['est_rem_sec'] = est_rem
                                else:
                                    # Never jump up; let it smoothly decrement
                                    task['est_rem_sec'] = max(0, prev_est - 1)
                                task['render_speed'] = f"{cur_speed:.1f}x"
                                task['stage'] = 'render'
                                task['message'] = f"⚙️ កំពុង {stage_name} វីដេអូ: {cur_sec/60:.1f}/{tot_sec/60:.1f} នាទី ({pct:.0f}%, ល្បឿន {cur_speed:.1f}x)..."
                                task['last_update'] = time.time()
                    
                    now = time.time()
                    if now - last_log_t >= 3.0:
                        last_log_t = now
                        mins_cur = cur_sec / 60.0
                        mins_tot = tot_sec / 60.0
                        log_callback(f"⚙️ កំពុង {stage_name} វីដេអូ: {mins_cur:.1f}/{mins_tot:.1f} នាទី ({pct:.0f}%, ល្បឿន {cur_speed:.1f}x, នៅសល់ ~{format_seconds_khmer(est_rem)})...")
                except Exception:
                    pass
    except Exception as e:
        print("[Server] Progress reader exception:", e)
        
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return -1, "Timeout expired"

    t_err.join(timeout=3)
    stderr_text = "".join(stderr_chunks)
    return proc.returncode, stderr_text

# Force UTF-8 encoding for standard streams to prevent Windows CP1252 crash on Khmer text
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass


# Automatically clean up any leftover temporary folders and files from previous runs
def cleanup_orphaned_temp_dirs():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        for item in os.listdir(base_dir):
            item_path = os.path.join(base_dir, item)
            if os.path.isdir(item_path) and (
                item.startswith("temp_srv_dub_") or 
                item.startswith("temp_backend_dub_") or 
                item.startswith("temp_transcribe_") or 
                item.startswith("temp_srv_trans_") or
                item.startswith("temp_split_srv_") or
                item.startswith("temp_batch_srv_") or
                item.startswith("temp_batch_tk_") or
                item.startswith("temp_dubbing") or
                item.startswith("temp_merged_srt_")
            ):
                try:
                    shutil.rmtree(item_path, ignore_errors=True)
                except Exception:
                    pass
            elif os.path.isfile(item_path) and (
                (item.startswith("temp_tts_") or item.startswith("test_voice_")) and item.endswith(".mp3")
            ):
                try:
                    os.remove(item_path)
                except Exception:
                    pass
    except Exception as e:
        print("Warning during temp cleanup:", e)

cleanup_orphaned_temp_dirs()

# Cloud deployment: PORT from environment (Railway/Render set this automatically)
PORT = int(os.environ.get('PORT', 8000))
HOST = '0.0.0.0'  # Listen on all interfaces (required for cloud)

# Track which TTS engine is currently active
active_tts_engine = "edge-tts"  # or "gtts"

# Function to auto-install required libraries
def install_requirements():
    libs_needed = []
    try:
        import edge_tts
    except ImportError:
        libs_needed.append('edge-tts')
    try:
        import imageio_ffmpeg
    except ImportError:
        libs_needed.append('imageio-ffmpeg')
    try:
        import gtts
    except ImportError:
        libs_needed.append('gtts')

    if libs_needed:
        print(f" Installing required libraries: {', '.join(libs_needed)}...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install"] + libs_needed)
            print(" Libraries installed successfully.")
        except Exception as e:
            print(f" Error installing libraries: {e}")
            print(f"Please run: pip install {' '.join(libs_needed)}")
    else:
        print(" All required libraries (edge-tts, imageio-ffmpeg, gtts) are already installed.")

install_requirements()

import imageio_ffmpeg
import edge_tts

# Try importing gTTS (fallback engine)
try:
    from gtts import gTTS as GoogleTTS
    GTTS_AVAILABLE = True
    print(" gTTS (Google TTS fallback) is available.")
except ImportError:
    GTTS_AVAILABLE = False
    print(" gTTS not available — only edge-tts will be used.")

# -----------------------------------------------------------------------
def detect_voice_gender(wav_path):
    """Detect speaker gender from a WAV file using a fast zero-crossing rate
    + energy-weighted approach — no O(n²) autocorrelation loops.
    Returns 'Female', 'Male', or 'Unknown'."""
    import wave
    import struct
    import math
    try:
        with wave.open(wav_path, 'rb') as w:
            num_frames = w.getnframes()
            sample_rate = w.getframerate()
            if num_frames == 0 or sample_rate == 0:
                return "Unknown"

            # Read up to 1 second of audio from the middle of the clip for robustness
            frames_to_read = min(num_frames, sample_rate)  # 1 second
            start_frame = max(0, (num_frames - frames_to_read) // 2)
            w.setpos(start_frame)
            data = w.readframes(frames_to_read)

        n = len(data) // 2
        if n < 100:
            return "Unknown"
        samples = struct.unpack(f'<{n}h', data[:n * 2])

        # RMS check — skip near-silent frames
        rms = math.sqrt(sum(s * s for s in samples) / n)
        if rms < 200:
            return "Unknown"

        # Zero-crossing rate (ZCR): females typically have higher ZCR due to higher F0
        # Use short frames (10ms) and compute mean ZCR
        frame_size = sample_rate // 100  # 10ms frames
        if frame_size < 2:
            frame_size = 2
        zcr_vals = []
        for fi in range(0, n - frame_size, frame_size):
            frame = samples[fi:fi + frame_size]
            zc = sum(
                1 for j in range(1, len(frame))
                if (frame[j] >= 0) != (frame[j - 1] >= 0)
            )
            zcr_vals.append(zc / frame_size)

        if not zcr_vals:
            return "Unknown"

        mean_zcr = sum(zcr_vals) / len(zcr_vals)
        # Thresholds calibrated for speech at 16kHz-24kHz:
        # Female ZCR typically > 0.08, Male < 0.065 (overlap zone → Unknown)
        if mean_zcr > 0.090:
            return "Female"
        elif mean_zcr < 0.060:
            return "Male"
        # Ambiguous — try simple spectral centroid heuristic on first 512 samples
        try:
            seg = samples[:min(512, n)]
            total_energy = sum(abs(s) for s in seg) or 1
            # Weight sample index by amplitude as crude spectral proxy
            centroid = sum(i * abs(s) for i, s in enumerate(seg)) / (total_energy * len(seg))
            # Higher centroid → higher frequency content → female
            if centroid > 0.50:
                return "Female"
            elif centroid < 0.35:
                return "Male"
        except Exception:
            pass
    except Exception as e:
        print("Gender detection exception:", e)
    return "Unknown"


_BEST_H264_ENCODER = None

def get_best_h264_encoder(ffmpeg_exe):
    global _BEST_H264_ENCODER
    if _BEST_H264_ENCODER is not None:
        return _BEST_H264_ENCODER
        
    # Check NVIDIA NVENC GPU first (only if NVIDIA GPU hardware is present and working)
    candidates = [
        ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23", "-pix_fmt", "yuv420p", "-g", "60", "-keyint_min", "30", "-avoid_negative_ts", "make_zero"]
    ]
    for cand in candidates:
        try:
            test_cmd = [ffmpeg_exe, "-y", "-f", "lavfi", "-i", "testsrc=duration=0.5:size=640x360:rate=24"] + cand + ["-f", "null", "-"]
            res = subprocess.run(test_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=4)
            if res.returncode == 0:
                _BEST_H264_ENCODER = cand
                print(f"[Server] Enabled Fast Video Encoder: {cand[1]}")
                return _BEST_H264_ENCODER
        except Exception:
            pass
            
    # Universal, ultra-fast CPU H.264 encoder (200x speed, 100% stable across all resolutions and containers, no QSV -22 crash)
    _BEST_H264_ENCODER = [
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-pix_fmt", "yuv420p", "-profile:v", "main",
        "-g", "60", "-keyint_min", "30", "-threads", "0", "-avoid_negative_ts", "make_zero"
    ]
    print("[Server] Using Universal High-Speed CPU Encoder: libx264 (ultrafast)")
    return _BEST_H264_ENCODER

def try_apply_faststart(video_file, ffmpeg_exe):
    """
    Safely attempts to apply MP4 +faststart (moving moov atom to beginning).
    If it fails for any reason (e.g. non-monotonic timestamps or Windows locking),
    the original valid file is safely preserved without failing the conversion.
    """
    if not video_file or not os.path.exists(video_file) or os.path.getsize(video_file) == 0:
        return
    fast_temp = video_file + ".fast.mp4"
    try:
        cmd = [
            ffmpeg_exe, "-y",
            "-i", video_file,
            "-c", "copy",
            "-movflags", "+faststart",
            fast_temp
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
        if res.returncode == 0 and os.path.exists(fast_temp) and os.path.getsize(fast_temp) > 0:
            shutil.move(fast_temp, video_file)
            print("[Server] Successfully applied +faststart to output video.")
        else:
            try:
                if os.path.exists(fast_temp): os.remove(fast_temp)
            except Exception: pass
    except Exception as e:
        print("[Server] Faststart skipped (original video preserved):", e)
        try:
            if os.path.exists(fast_temp): os.remove(fast_temp)
        except Exception: pass


def normalize_edge_pitch(pitch_input):
    """
    Edge-TTS strictly requires pitch in the format '^[+-]\d+Hz$' (e.g. '+15Hz', '-15Hz').
    It rejects '%' with ValueError: Invalid pitch '+10%'.
    This helper converts any input ('+10%', '10%', '+10', '+10Hz', '-10%') to valid Edge-TTS pitch.
    Note: For speech, 10% change corresponds to ~15Hz shift in human fundamental frequency.
    """
    if not pitch_input:
        return None
    s = str(pitch_input).strip()
    if s in ("0", "+0", "-0", "0%", "+0%", "-0%", "0Hz", "+0Hz", "-0Hz", "none", "None", ""):
        return None

    # If already ending in Hz, validate format
    if s.endswith('Hz') or s.endswith('hz'):
        clean = s[:-2].strip()
        m = re.match(r'^([+-]?)(\d+)$', clean)
        if m:
            sign = m.group(1) or "+"
            val = int(m.group(2))
            return f"{sign}{val}Hz" if val != 0 else None

    # If percentage e.g. '+10%', '+20%', '-10%'
    m = re.match(r'^([+-]?)(\d+)', s)
    if m:
        sign = m.group(1) or "+"
        pct_val = int(m.group(2))
        if pct_val == 0:
            return None
        # Map 10% -> 15Hz, 20% -> 30Hz for a clearly audible and natural pitch shift
        hz_val = int(round(pct_val * 1.5))
        return f"{sign}{hz_val}Hz"

    return None


def normalize_voice_name(voice_name):
    if not voice_name:
        return 'km-KH-PisethNeural'
    v = str(voice_name).strip()
    v_low = v.lower()
    if 'sreymom' in v_low or 'sokha' in v_low or 'female' in v_low or 'ស្រី' in v_low:
        return 'km-KH-SreymomNeural'
    if 'piseth' in v_low or 'chitra' in v_low or 'male' in v_low or 'ប្រុស' in v_low:
        return 'km-KH-PisethNeural'
    return v


# Multi-engine TTS generator
# Tries edge-tts (Piseth/Sreymom) first; falls back to gTTS if it fails.
# -----------------------------------------------------------------------
async def generate_tts_mp3(text: str, edge_voice: str, rate_param: str, output_path: str, pitch_param: str = None) -> str:
    """
    Generate TTS audio. Returns 'edge-tts' or 'gtts' to indicate which engine succeeded.
    Raises Exception if both fail.
    """
    global active_tts_engine

    edge_pitch = normalize_edge_pitch(pitch_param)

    # --- Attempt 1: edge-tts (Piseth / Sreymom Neural voices) ---
    try:
        if edge_pitch:
            communicate = edge_tts.Communicate(text, edge_voice, rate=rate_param, pitch=edge_pitch)
        else:
            communicate = edge_tts.Communicate(text, edge_voice, rate=rate_param)
        await asyncio.wait_for(communicate.save(output_path), timeout=25.0)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            active_tts_engine = "edge-tts"
            return "edge-tts"
        else:
            raise RuntimeError("edge-tts produced empty file")
    except Exception as e:
        print(f"  [TTS] edge-tts failed ({type(e).__name__}: {e}), trying gTTS fallback...")
        # Fallback to no-pitch if edge-tts failed
        if edge_pitch:
            try:
                print("  [TTS] Retrying edge-tts without pitch parameter...")
                communicate = edge_tts.Communicate(text, edge_voice, rate=rate_param)
                await asyncio.wait_for(communicate.save(output_path), timeout=25.0)
                if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                    active_tts_engine = "edge-tts"
                    return "edge-tts"
            except Exception as e_retry:
                print(f"  [TTS] Retry edge-tts without pitch also failed: {e_retry}")

    # --- Attempt 2: gTTS (Google Translate TTS, Khmer locale) ---
    if GTTS_AVAILABLE:
        try:
            def _gtts_sync():
                tts = GoogleTTS(text=text, lang='km', slow=False)
                tts.save(output_path)
            loop = asyncio.get_event_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, _gtts_sync),
                timeout=20.0
            )
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                active_tts_engine = "gtts"
                return "gtts"
            else:
                raise RuntimeError("gTTS produced empty file")
        except Exception as e2:
            print(f"  [TTS] gTTS fallback also failed: {e2}")

    raise RuntimeError("All TTS engines failed. Check network connection and try again.")


# SRT parsing helpers
def time_to_ms(h, m, s, ms):
    return ((h * 3600) + (m * 60) + s) * 1000 + ms

def parse_srt(srt_path):
    with open(srt_path, 'r', encoding='utf-8') as f:
        content = f.read()
    content = content.replace('\r\n', '\n')
    
    # Strip markdown block wrappers or conversational intro text from Gemini
    lines = content.split('\n')
    srt_start_idx = -1
    for idx, line in enumerate(lines):
        if '-->' in line:
            if idx > 0 and lines[idx - 1].strip().isdigit():
                srt_start_idx = idx - 1
            else:
                srt_start_idx = idx
            break
            
    if srt_start_idx != -1:
        lines = lines[srt_start_idx:]
        
    clean_lines = []
    for line in lines:
        if line.strip().startswith('```'):
            continue
        clean_lines.append(line)
        
    reconstructed = "\n".join(clean_lines).strip()
    blocks = re.split(r'\n\s*\n', reconstructed)
    subtitles = []
    for block in blocks:
        blk_lines = [l.strip() for l in block.split('\n') if l.strip()]
        if len(blk_lines) >= 2:
            time_line = ""
            text_lines = []
            
            time_idx = -1
            for l_idx, l in enumerate(blk_lines):
                if '-->' in l:
                    time_idx = l_idx
                    time_line = l
                    break
            
            if time_idx != -1:
                text_lines = blk_lines[time_idx + 1:]
                text = " ".join(text_lines).strip()
                match = re.match(
                    r'(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})',
                    time_line
                )
                if match:
                    start_ms = time_to_ms(int(match.group(1)), int(match.group(2)),
                                          int(match.group(3)), int(match.group(4)))
                    end_ms = time_to_ms(int(match.group(5)), int(match.group(6)),
                                        int(match.group(7)), int(match.group(8)))
                    
                    idx_val = str(len(subtitles) + 1)
                    if time_idx > 0:
                        idx_val = blk_lines[time_idx - 1]
                    subtitles.append({
                        'id': idx_val,
                        'start_ms': start_ms,
                        'end_ms': end_ms,
                        'text': text
                    })
    return subtitles

# Custom multipart form parser (no external library required)
def parse_multipart(body_bytes, boundary):
    boundary_bytes = b'--' + boundary.encode('utf-8')
    parts = body_bytes.split(boundary_bytes)
    form_data = {}
    files = {}
    
    for part in parts:
        if not part or part == b'--\r\n' or part == b'--':
            continue
        if part.startswith(b'\r\n'):
            part = part[2:]
        header_end = part.find(b'\r\n\r\n')
        if header_end == -1:
            continue
        headers = part[:header_end].decode('utf-8', errors='ignore')
        body = part[header_end+4:]
        if body.endswith(b'\r\n'):
            body = body[:-2]
            
        name_match = re.search(r'name=["\']?([^"\';\r\n]+)["\']?', headers, re.IGNORECASE)
        if name_match:
            name = name_match.group(1).strip()
            filename_match = re.search(r'filename=["\']?([^"\';\r\n]+)["\']?', headers, re.IGNORECASE)
            if filename_match:
                files[name] = {
                    'filename': os.path.basename(filename_match.group(1).strip()),
                    'content': body
                }
            elif name in ('video', 'srt', 'image_intro', 'image_outro') and len(body) > 0:
                files[name] = {
                    'filename': f"{name}.png" if 'image' in name else f"{name}.bin",
                    'content': body
                }
            else:
                form_data[name] = body.decode('utf-8', errors='ignore')
    return form_data, files

# Helper: get WAV duration accurately using WAV header (fast) or ffprobe (fallback)
def get_audio_duration_ms(ffmpeg_exe, wav_path):
    """Get accurate audio duration in ms using fast WAV header calculation first, falling back to ffprobe."""
    try:
        file_size = os.path.getsize(wav_path)
        with open(wav_path, 'rb') as f:
            f.seek(24)  # Sample rate offset in WAV header
            sample_rate = int.from_bytes(f.read(4), 'little')
            f.seek(34)  # Bits per sample offset
            bits_per_sample = int.from_bytes(f.read(2), 'little')
            f.seek(22)  # Num channels
            num_channels = int.from_bytes(f.read(2), 'little')
        bytes_per_sec = sample_rate * num_channels * (bits_per_sample // 8)
        data_bytes = max(0, file_size - 44)
        if bytes_per_sec > 0:
            return int((data_bytes / bytes_per_sec) * 1000)
    except Exception:
        pass

    # Fallback to ffprobe
    if ffmpeg_exe == "ffmpeg":
        ffprobe_exe = "ffprobe"
    else:
        ffprobe_exe = ffmpeg_exe.replace('ffmpeg', 'ffprobe')
    
    import shutil
    if shutil.which(ffprobe_exe) or os.path.exists(ffprobe_exe):
        try:
            cmd_exe = ffprobe_exe if os.path.exists(ffprobe_exe) else "ffprobe"
            result = subprocess.run(
                [cmd_exe, "-v", "quiet", "-print_format", "json",
                 "-show_streams", wav_path],
                capture_output=True, text=True, timeout=15
            )
            import json
            info = json.loads(result.stdout)
            for stream in info.get('streams', []):
                duration = stream.get('duration')
                if duration:
                    return int(float(duration) * 1000)
        except Exception:
            pass
    return 0


# Helper: get total video duration in seconds via ffprobe/ffmpeg
def get_video_duration(video_path, ffmpeg_exe):
    if ffmpeg_exe == "ffmpeg":
        ffprobe_exe = "ffprobe"
    else:
        ffprobe_exe = ffmpeg_exe.replace('ffmpeg', 'ffprobe')
    try:
        cmd = [
            ffprobe_exe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        if res.returncode == 0 and res.stdout.strip():
            return float(res.stdout.strip())
    except Exception:
        pass

    # Fallback: get duration by parsing ffmpeg -i output (highly compatible)
    try:
        cmd = [ffmpeg_exe, "-i", video_path]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        output = res.stderr
        match = re.search(r"Duration:\s*(\d{2}):(\d{2}):(\d{2})\.(\d{2})", output)
        if match:
            hours = int(match.group(1))
            minutes = int(match.group(2))
            seconds = int(match.group(3))
            hundredths = int(match.group(4))
            total_seconds = hours * 3600 + minutes * 60 + seconds + hundredths / 100.0
            print("[Server] Probed video duration using ffmpeg fallback:", total_seconds)
            return total_seconds
    except Exception as e:
        print("[Server] Failed probing video duration with ffmpeg fallback:", e)
    return None

def get_video_fps(video_path, ffmpeg_exe):
    """Detects video frame rate to preserve original 24/25fps without useless frame padding."""
    try:
        cmd = [ffmpeg_exe, "-i", video_path]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        m = re.search(r'(\d+(?:\.\d+)?)\s*fps', res.stderr)
        if m:
            val = float(m.group(1))
            if 15.0 <= val <= 60.0:
                if val > 32.0:
                    return 30
                return int(round(val))
    except Exception:
        pass
    return 30


def get_video_dimensions(video_path, ffmpeg_exe):
    ffprobe_exe = shutil.which("ffprobe") or ffmpeg_exe.replace("ffmpeg", "ffprobe")
    if ffprobe_exe and os.path.exists(ffprobe_exe):
        try:
            cmd = [
                ffprobe_exe, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=s=x:p=0",
                video_path
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            if res.returncode == 0 and 'x' in res.stdout:
                w, h = res.stdout.strip().split('x')
                return int(w), int(h)
        except Exception as e:
            print("[Server] ffprobe dimensions error:", e)

    # Robust fallback: parse ffmpeg -i output directly
    try:
        cmd = [ffmpeg_exe, "-i", video_path]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        output = res.stderr
        rotated = False
        rot_match = re.search(r'rotate\s*:\s*(\d+)', output, re.IGNORECASE) or re.search(r'displaymatrix:\s*rotation\s*of\s*(-?\d+)', output, re.IGNORECASE)
        if rot_match:
            try:
                deg = abs(int(float(rot_match.group(1))))
                if deg in (90, 270):
                    rotated = True
            except Exception:
                pass

        m = re.search(r'Stream\s*#\d+:\d+.*Video:.*?[,\s](\d{2,5})x(\d{2,5})', output)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            if rotated:
                w, h = h, w
            print(f"[Server] Probed video dimensions using ffmpeg: {w}x{h} (rotated={rotated})")
            return w, h
    except Exception as e:
        print("[Server] Failed probing video dimensions with ffmpeg:", e)

    return 1920, 1080


def check_video_has_audio(video_path, ffmpeg_exe):
    """Detect if a video file has an audio stream."""
    if not os.path.exists(video_path) or os.path.getsize(video_path) == 0:
        return False
    ffprobe_exe = shutil.which("ffprobe") or ffmpeg_exe.replace("ffmpeg", "ffprobe")
    if ffprobe_exe and os.path.exists(ffprobe_exe):
        try:
            cmd = [
                ffprobe_exe, "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_type",
                "-of", "csv=p=0",
                video_path
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            if res.returncode == 0 and "audio" in res.stdout.lower():
                return True
        except Exception:
            pass

    try:
        cmd = [ffmpeg_exe, "-i", video_path]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        if "Audio:" in res.stderr:
            return True
    except Exception:
        pass
    return False


def robust_concat_videos(video_paths, output_path, ffmpeg_exe, log_callback, fix_freeze=False):
    """
    Robustly concatenates multiple video files.
    Specifically solves mobile phone video issues:
    - Codec mismatch (HEVC/H.265 from iPhone + H.264)
    - Mixed resolutions & Aspect ratios (auto-scales and pads with letterboxing to match standard resolution)
    - Clips without audio / silent recordings (synthesizes silent stereo audio)
    - Sample rate differences (normalizes to 48kHz stereo)
    - Variable frame rate (VFR) -> converts to 30fps CFR
    """
    if not video_paths:
        log_callback("❌ គ្មានឯកសារវីដេអូសម្រាប់តភ្ជាប់ទេ")
        return False

    # 1. Validate all files exist and are not empty (critical for mobile uploads)
    for idx, vp in enumerate(video_paths):
        if not os.path.exists(vp):
            log_callback(f"❌ រកមិនឃើញឯកសារវីដេអូទី {idx+1} ({os.path.basename(vp)}) — សូមពិនិត្យការ Upload ពីទូរសព្ទ")
            return False
        if os.path.getsize(vp) == 0:
            log_callback(f"❌ វីដេអូទី {idx+1} ({os.path.basename(vp)}) មានទំហំ 0 bytes (អាចដាច់ការ Upload ពីទូរសព្ទ)")
            return False

    if len(video_paths) == 1:
        shutil.copy(video_paths[0], output_path)
        return True

    batch_temp_dir = f"temp_concat_{uuid.uuid4()}"
    os.makedirs(batch_temp_dir, exist_ok=True)

    try:
        # Step 2: Stream copy vs Safe Re-encode
        # When fix_freeze=True, skip stream copy completely to prevent stuttering/glitching at clip junctions!
        res_ok = False
        if not fix_freeze:
            try:
                log_callback("⚡ កំពុងសាកល្បងតភ្ជាប់វីដេអូល្បឿនលឿន (Fast Stream Copy 1s)...")
                concat_txt = os.path.join(batch_temp_dir, "concat_stream.txt")
                with open(concat_txt, "w", encoding="utf-8") as f:
                    for vp in video_paths:
                        escaped = os.path.abspath(vp).replace('\\', '/').replace("'", "'\\''")
                        f.write(f"file '{escaped}'\n")

                cmd_copy = [
                    ffmpeg_exe, "-y",
                    "-fflags", "+genpts+discardcorrupt",
                    "-f", "concat", "-safe", "0",
                    "-i", concat_txt,
                    "-c", "copy",
                    "-avoid_negative_ts", "make_zero",
                    output_path
                ]
                res = subprocess.run(cmd_copy, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)
                if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                    actual_dur = get_video_duration(output_path, ffmpeg_exe) or 0
                    total_expected_dur = sum((get_video_duration(vp, ffmpeg_exe) or 0) for vp in video_paths)
                    if total_expected_dur > 0 and actual_dur < (total_expected_dur - 4.0):
                        log_callback(f"⚠️ Fast Stream Copy តភ្ជាប់បានតែ {actual_dur/60:.1f} នាទី / {total_expected_dur/60:.1f} នាទី — ប្តូរទៅ Safe Concat...")
                        res_ok = False
                    else:
                        res_ok = True
                        log_callback(f"⚡ បានតភ្ជាប់វីដេអូចំនួន {len(video_paths)} ជោគជ័យពេញលេញ {actual_dur/60:.1f} នាទី (Stream Copy 1s)!")
            except Exception:
                res_ok = False
        else:
            log_callback("🛡️ បើកមុខងារ Safe Concat (Re-encode 30fps CFR + Reset PTS ដើម្បីការពារកុំឱ្យទាក់រូបភាព)...")

        # Step 3: If stream copy failed or fix_freeze is true, perform Robust Filter Concat
        if not res_ok:
            if os.path.exists(output_path):
                try: os.remove(output_path)
                except Exception: pass

            log_callback("🛡️ កំពុងតភ្ជាប់ និង Re-encode ដោយសុវត្ថិភាព (Safe Concat សម្រួល Resolution & Audio កុំឱ្យ error)...")

            # Determine best target resolution (from the first valid video or standard 1920x1080)
            tw, th = 1920, 1080
            for vp in video_paths:
                w, h = get_video_dimensions(vp, ffmpeg_exe)
                if w > 0 and h > 0:
                    tw, th = w - (w % 2), h - (h % 2)
                    break

            log_callback(f"📐 សម្រួលទំហំវីដេអូទាំងអស់ឱ្យស្មើគ្នា: {tw}x{th} (CFR 30fps)...")

            # Build filter_complex with auto-scale, padding, and audio normalization
            filter_parts = []
            concat_streams = []

            fallback_cmd = [ffmpeg_exe, "-y", "-fflags", "+genpts+discardcorrupt"]
            for idx, vp in enumerate(video_paths):
                fallback_cmd.extend(["-i", vp])
                has_audio = check_video_has_audio(vp, ffmpeg_exe)
                dur = get_video_duration(vp, ffmpeg_exe) or 10.0

                # Video filter: scale preserving aspect ratio, pad to tw:th, set SAR 1, 30fps CFR, reset PTS, yuv420p
                filter_parts.append(
                    f"[{idx}:v]scale={tw}:{th}:force_original_aspect_ratio=decrease,"
                    f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,settb=AVTB,setpts=PTS-STARTPTS,format=yuv420p[v{idx}]"
                )

                # Audio filter: if exists normalize sample rate, otherwise generate silence
                if has_audio:
                    filter_parts.append(
                        f"[{idx}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,asetpts=PTS-STARTPTS[a{idx}]"
                    )
                else:
                    filter_parts.append(
                        f"anullsrc=channel_layout=stereo:sample_rate=48000:d={dur}[a{idx}]"
                    )

                concat_streams.append(f"[v{idx}][a{idx}]")

            concat_filter = f"{';'.join(filter_parts)};{''.join(concat_streams)}concat=n={len(video_paths)}:v=1:a=1[outv][outa]"

            best_enc = get_best_h264_encoder(ffmpeg_exe)
            fallback_cmd.extend([
                "-filter_complex", concat_filter,
                "-map", "[outv]", "-map", "[outa]",
                "-vsync", "cfr", "-r", "30",
                "-max_muxing_queue_size", "4096"
            ] + best_enc + [
                "-c:a", "aac", "-b:a", "192k",
                "-avoid_negative_ts", "make_zero",
                output_path
            ])

            tot_dur = sum((get_video_duration(vp, ffmpeg_exe) or 0) for vp in video_paths)
            retcode, err_txt = run_ffmpeg_with_progress(
                fallback_cmd, tot_dur, log_callback,
                task_id=None, base_pct=5.0, max_pct=95.0, timeout=1800, stage_name="តភ្ជាប់វីដេអូ"
            )

            if retcode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                clean_err = "\n".join([line for line in err_txt.splitlines() if "error" in line.lower() or "fatal" in line.lower()][-3:])
                log_callback(f"❌ បរាជ័យក្នុងការតភ្ជាប់វីដេអូបញ្ចូលគ្នា: {clean_err or err_txt[-200:]}")
                return False

        return True
    finally:
        shutil.rmtree(batch_temp_dir, ignore_errors=True)



def get_best_font_file(text="", preferred_font_name=""):
    """Find or auto-download the best Khmer-capable font for FFmpeg drawtext.
    Supports: Windows, Linux (Debian/Ubuntu/Cloud), macOS, Android (Termux), Docker.
    Falls back to auto-downloading NotoSansKhmer from GitHub if no Khmer font found.
    """
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    _cache_dir  = os.path.join(_script_dir, "fonts_cache")
    _cached_noto = os.path.join(_cache_dir, "NotoSansKhmer-Regular.ttf")

    def _fmt(p):
        return p.replace('\\', '/').replace(':', '\\:')

    font_name_map = {
        'KhmerOSniroth': [
            r"C:\Windows\Fonts\KhmerOSniroth.ttf",
            r"C:\Windows\Fonts\KhmerOS_niroth.ttf",
            "/usr/share/fonts/truetype/khmeros/KhmerOSniroth.ttf",
            "/usr/share/fonts/khmeros/KhmerOSniroth.ttf",
        ],
        'KhmerOSbattambang': [
            r"C:\Windows\Fonts\KhmerOSbattambang.ttf",
            "/usr/share/fonts/truetype/khmeros/KhmerOSbattambang.ttf",
            "/usr/share/fonts/khmeros/KhmerOSbattambang.ttf",
        ],
        'KhmerOSfasthand': [
            r"C:\Windows\Fonts\KhmerOSfasthand.ttf",
            "/usr/share/fonts/truetype/khmeros/KhmerOSfasthand.ttf",
            "/usr/share/fonts/khmeros/KhmerOSfasthand.ttf",
        ],
        'KhmerOScontent': [
            r"C:\Windows\Fonts\KhmerOScontent.ttf",
            "/usr/share/fonts/truetype/khmeros/KhmerOScontent.ttf",
            "/usr/share/fonts/khmeros/KhmerOScontent.ttf",
        ],
        'KhmerOS': [
            r"C:\Windows\Fonts\KhmerOS.ttf",
            "/usr/share/fonts/truetype/khmeros/KhmerOS.ttf",
            "/usr/share/fonts/khmeros/KhmerOS.ttf",
        ],
        'KhmerUI': [
            r"C:\Windows\Fonts\KhmerUI.ttf",
            "/usr/share/fonts/truetype/khmeros/KhmerUI.ttf",
        ],
        'NotoSansKhmer': [
            r"C:\Windows\Fonts\NotoSansKhmer-Regular.ttf",
            r"C:\Windows\Fonts\Noto Sans Khmer.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansKhmer-Regular.ttf",
            "/usr/share/fonts/noto/NotoSansKhmer-Regular.ttf",
            "/usr/share/fonts/NotoSansKhmer-Regular.ttf",
            "/system/fonts/NotoSansKhmer-Regular.ttf",
            "/usr/local/share/fonts/NotoSansKhmer-Regular.ttf",
            _cached_noto,
        ],
    }

    khmer_candidates = [
        r"C:\Windows\Fonts\KhmerOSniroth.ttf",
        r"C:\Windows\Fonts\KhmerOS_niroth.ttf",
        r"C:\Windows\Fonts\KhmerOSbattambang.ttf",
        r"C:\Windows\Fonts\KhmerOScontent.ttf",
        r"C:\Windows\Fonts\KhmerOSfasthand.ttf",
        r"C:\Windows\Fonts\KhmerOS.ttf",
        r"C:\Windows\Fonts\KhmerUI.ttf",
        r"C:\Windows\Fonts\NotoSansKhmer-Regular.ttf",
        r"C:\Windows\Fonts\Noto Sans Khmer.ttf",
        r"C:\Windows\Fonts\daunpenh.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansKhmer-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansKhmer-Bold.ttf",
        "/usr/share/fonts/noto/NotoSansKhmer-Regular.ttf",
        "/usr/share/fonts/NotoSansKhmer-Regular.ttf",
        "/usr/share/fonts/truetype/khmeros/KhmerOSbattambang.ttf",
        "/usr/share/fonts/truetype/khmeros/KhmerOS.ttf",
        "/usr/share/fonts/khmeros/KhmerOS.ttf",
        "/usr/local/share/fonts/NotoSansKhmer-Regular.ttf",
        "/Library/Fonts/NotoSansKhmer-Regular.ttf",
        "/System/Library/Fonts/Supplemental/NotoSansKhmer-Regular.ttf",
        "/system/fonts/NotoSansKhmer-Regular.ttf",
        "/data/data/com.termux/files/usr/share/fonts/NotoSansKhmer-Regular.ttf",
        _cached_noto,
    ]
    general_candidates = [
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]

    # Step 1: Preferred font
    if preferred_font_name and preferred_font_name != 'auto':
        for f in font_name_map.get(preferred_font_name, []):
            if os.path.exists(f):
                print(f"[FONT] Using preferred font: {f}")
                return _fmt(f)

    # Step 2: Any available Khmer font
    has_khmer = bool(re.search(r'[\u1780-\u17ff]', text)) if text else True
    if has_khmer:
        for f in khmer_candidates:
            if os.path.exists(f):
                print(f"[FONT] Found Khmer font: {f}")
                return _fmt(f)

        # Step 3: Scan Windows Fonts folder
        if os.path.exists(r"C:\Windows\Fonts"):
            for fn in os.listdir(r"C:\Windows\Fonts"):
                fn_lower = fn.lower()
                if fn_lower.endswith(".ttf") and any(k in fn_lower for k in ('khmer', 'khmeros', 'noto')):
                    full = os.path.join(r"C:\Windows\Fonts", fn)
                    print(f"[FONT] Found Khmer font (scan): {full}")
                    return _fmt(full)

        # Step 4: Auto-download NotoSansKhmer from GitHub
        noto_url = (
            "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/"
            "NotoSansKhmer/NotoSansKhmer-Regular.ttf"
        )
        try:
            os.makedirs(_cache_dir, exist_ok=True)
            print("[FONT] No Khmer font found locally. Auto-downloading NotoSansKhmer...")
            req = urllib.request.Request(noto_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp, open(_cached_noto, 'wb') as out:
                out.write(resp.read())
            if os.path.exists(_cached_noto) and os.path.getsize(_cached_noto) > 10000:
                print(f"[FONT] Downloaded NotoSansKhmer to {_cached_noto}")
                return _fmt(_cached_noto)
        except Exception as dl_err:
            print(f"[FONT] Auto-download failed: {dl_err}")

    # Step 5: General font fallback
    for f in general_candidates:
        if os.path.exists(f):
            print(f"[FONT] Using general font: {f}")
            return _fmt(f)

    # Step 6: Walk any system font directory
    for search_dir in ["/usr/share/fonts", "/usr/local/share/fonts", r"C:\Windows\Fonts"]:
        if os.path.isdir(search_dir):
            for root_d, _, files in os.walk(search_dir):
                for fn in files:
                    if fn.lower().endswith(".ttf"):
                        full = os.path.join(root_d, fn)
                        print(f"[FONT] Last-resort font: {full}")
                        return _fmt(full)

    print("[FONT] WARNING: No font found. Text overlay will likely fail.")
    return None



def normalize_ass_font(font_name):
    """Map internal or system font IDs to actual font family names recognized by libass."""
    mapping = {
        'khmerosniroth': 'Khmer OS Niroth',
        'khmeros_niroth': 'Khmer OS Niroth',
        'khmerosbattambang': 'Khmer OS Battambang',
        'khmeros_battambang': 'Khmer OS Battambang',
        'khmeroscontent': 'Khmer OS Content',
        'khmerosfasthand': 'Khmer OS Fasthand',
        'khmerosfreehand': 'Khmer OS Freehand',
        'khmerossiemreap': 'Khmer OS Siemreap',
        'khmerosmuol': 'Khmer OS Muol',
        'khmerosmuollight': 'Khmer OS Muol Light',
        'khmeros': 'Khmer OS',
        'khmerui': 'Khmer UI',
        'notosanskhmer': 'Noto Sans Khmer',
        'daunpenh': 'DaunPenh',
        'leelawadee': 'Leelawadee UI',
    }
    if not font_name or str(font_name).strip().lower() in ('auto', 'default'):
        return 'Khmer OS Battambang'
    c = str(font_name).strip().lower().replace(' ', '').replace('-', '').replace('_', '')
    for k, v in mapping.items():
        if k in c:
            return v
    return str(font_name).strip()


def create_video_ass_file(output_path, video_w, video_h, video_dur_sec,
                          text_overlay="", text_timing="start_30", text_pos="bottom_center",
                          text_size="32", text_font="KhmerOSniroth", text_color="white",
                          text_stroke_color="black", text_stroke_width="2",
                          outro_text="", outro_pos="bottom_center", outro_size="42",
                          outro_font="KhmerOSniroth", outro_color="white",
                          outro_stroke_color="black", outro_stroke_width="3"):
    """Generate an ASS subtitle file containing styles and events for overlay and outro text.
    libass utilizes HarfBuzz which guarantees 100% accurate Khmer complex text shaping
    (subscript consonants / ជើងតួអក្សរ and dependent vowels stack and blend correctly)."""
    color_table = {
        'white':   '&H00FFFFFF', 'yellow':  '&H0000FFFF',
        'cyan':    '&H00FFFF00', 'lime':    '&H0000FF00',
        'green':   '&H0000FF00', 'orange':  '&H000080FF',
        'red':     '&H000000FF', 'hotpink': '&H00B469FF',
        'pink':    '&H00CBC0FF', 'black':   '&H00000000',
        'blue':    '&H00FF0000', 'none':    '&H00000000',
    }

    def hex_to_ass(h, default='&H00FFFFFF'):
        if not h:
            return default
        h = str(h).strip()
        if h in color_table:
            return color_table[h]
        clean_h = h.lstrip('#').strip()
        if len(clean_h) >= 6:
            r = clean_h[0:2]
            g = clean_h[2:4]
            b = clean_h[4:6]
            return f"&H00{b}{g}{r}".upper()
        return default

    align_map = {
        'bottom_center': 2, 'top_center': 8, 'center': 5,
        'bottom_right':  3, 'bottom_left': 1,
        'top_right':     9, 'top_left':   7,
    }

    def fmt_time(seconds):
        seconds = max(0.0, float(seconds))
        h  = int(seconds // 3600)
        m  = int((seconds % 3600) // 60)
        s  = seconds % 60
        cs = int(round((s % 1) * 100))
        if cs >= 100:
            cs = 99
        s  = int(s)
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    def escape_ass(txt):
        return str(txt).replace('\\', '\\\\').replace('{', '\\{').replace('}', '\\}').replace('\n', '\\N').replace('\r', '')

    vw = int(video_w) if video_w else 1280
    vh = int(video_h) if video_h else 720
    dur = float(video_dur_sec) if (video_dur_sec and float(video_dur_sec) > 0) else 60.0

    styles = []
    events = []

    # 1. Overlay Text
    if text_overlay and text_overlay.strip():
        txt = text_overlay.strip()
        f_name = normalize_ass_font(text_font)
        try:
            f_size = max(12, int(text_size))
        except Exception:
            f_size = 36
        pri_col = hex_to_ass(text_color, '&H00FFFFFF')
        strk_col = hex_to_ass(text_stroke_color, '&H00000000')
        try:
            strk_w = int(text_stroke_width or 2)
        except Exception:
            strk_w = 2

        pos = text_pos or 'bottom_center'
        align = align_map.get(pos, 2)
        margin_v = 40 if align in (1, 2, 3, 7, 8, 9) else 0

        if strk_w > 0 and text_stroke_color and str(text_stroke_color).strip().lower() not in ('none', ''):
            b_style = 1
            outl = strk_w
            shadow = 1
            back_c = '&H80000000'
        else:
            b_style = 3  # opaque box
            outl = 4
            shadow = 0
            back_c = '&H99000000'

        styles.append(
            f"Style: OverlayStyle,{f_name},{f_size},{pri_col},&H000000FF,{strk_col},{back_c},"
            f"-1,0,0,0,100,100,0,0,{b_style},{outl},{shadow},{align},30,30,{margin_v},1"
        )

        safe_txt = escape_ass(txt)
        if text_timing == 'start_30':
            events.append(f"Dialogue: 0,{fmt_time(0)},{fmt_time(min(30.0, dur))},OverlayStyle,,0,0,0,,{safe_txt}")
        elif text_timing == 'end_30':
            st = max(0.0, dur - 30.0)
            events.append(f"Dialogue: 0,{fmt_time(st)},{fmt_time(dur)},OverlayStyle,,0,0,0,,{safe_txt}")
        elif text_timing == 'start_end_30':
            events.append(f"Dialogue: 0,{fmt_time(0)},{fmt_time(min(30.0, dur))},OverlayStyle,,0,0,0,,{safe_txt}")
            if dur > 30.0:
                st = max(0.0, dur - 30.0)
                events.append(f"Dialogue: 0,{fmt_time(st)},{fmt_time(dur)},OverlayStyle,,0,0,0,,{safe_txt}")
        elif text_timing == 'full':
            events.append(f"Dialogue: 0,{fmt_time(0)},{fmt_time(dur)},OverlayStyle,,0,0,0,,{safe_txt}")
        else:
            events.append(f"Dialogue: 0,{fmt_time(0)},{fmt_time(min(30.0, dur))},OverlayStyle,,0,0,0,,{safe_txt}")

    # 2. Outro Text (last 30 seconds)
    if outro_text and outro_text.strip():
        txt = outro_text.strip()
        f_name = normalize_ass_font(outro_font)
        try:
            f_size = max(14, int(outro_size))
        except Exception:
            f_size = 42
        pri_col = hex_to_ass(outro_color, '&H00FFFFFF')
        strk_col = hex_to_ass(outro_stroke_color, '&H00000000')
        try:
            strk_w = int(outro_stroke_width or 3)
        except Exception:
            strk_w = 3

        pos = outro_pos or 'bottom_center'
        align = align_map.get(pos, 2)
        margin_v = 40 if align in (1, 2, 3, 7, 8, 9) else 0

        if strk_w > 0 and outro_stroke_color and str(outro_stroke_color).strip().lower() not in ('none', ''):
            b_style = 1
            outl = strk_w
            shadow = 1
            back_c = '&H80000000'
        else:
            b_style = 3
            outl = 4
            shadow = 0
            back_c = '&H99000000'

        styles.append(
            f"Style: OutroStyle,{f_name},{f_size},{pri_col},&H000000FF,{strk_col},{back_c},"
            f"-1,0,0,0,100,100,0,0,{b_style},{outl},{shadow},{align},30,30,{margin_v},1"
        )

        safe_txt = escape_ass(txt)
        st = max(0.0, dur - 30.0)
        events.append(f"Dialogue: 1,{fmt_time(st)},{fmt_time(dur)},OutroStyle,,0,0,0,,{safe_txt}")

    if not events:
        return None

    ass_content = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {vw}\n"
        f"PlayResY: {vh}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + "\n".join(styles) + "\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(events) + "\n"
    )

    with open(output_path, 'w', encoding='utf-8-sig') as f:
        f.write(ass_content)

    return output_path


def get_overlay_coords(pos, margin=20):
    pos = (pos or "").lower()
    if pos == "top_left":
        return f"{margin}:{margin}"
    elif pos == "top_center":
        return f"(W-w)/2:{margin}"
    elif pos == "top_right":
        return f"W-w-{margin}:{margin}"
    elif pos == "bottom_left":
        return f"{margin}:H-h-{margin}"
    elif pos == "bottom_center":
        return f"(W-w)/2:H-h-{margin}"
    elif pos == "bottom_right":
        return f"W-w-{margin}:H-h-{margin}"
    elif pos == "center":
        return "(W-w)/2:(H-h)/2"
    else:
        return f"(W-w)/2:H-h-{margin}"

def calc_scaled_image_dims(image_path, vid_w, vid_h, pct):
    """
    Calculate target_w and target_h for image overlay respecting natural aspect ratio:
    - pct: fraction between 0.05 and 1.0 (e.g. 0.25 = 25%)
    - If image is a wide banner (aspect ratio >= 1.2): width is vid_w * pct, height scales accordingly
    - If image is square or vertical (aspect ratio < 1.2): height/width scales relative to min(vid_w, vid_h) * pct
    - Always clamps within 90% of screen to avoid any overflow or clipping
    - Both width and height are forced to even numbers for H.264 compatibility
    """
    img_w, img_h = 0, 0
    if image_path and os.path.exists(image_path):
        try:
            from PIL import Image
            with Image.open(image_path) as im:
                img_w, img_h = im.size
        except Exception:
            pass

    if img_w <= 0 or img_h <= 0:
        target_w = max(32, int(vid_w * pct))
        target_h = max(32, int(vid_h * pct))
        return (target_w // 2) * 2, (target_h // 2) * 2

    img_ar = img_w / float(img_h)
    
    if pct >= 0.98:
        # 100% Full screen overlay: scale to fill or fit video screen
        scale = min(vid_w / float(img_w), vid_h / float(img_h))
        target_w = int(img_w * scale)
        target_h = int(img_h * scale)
        # If aspect ratio is close to video aspect ratio, match exact video dimensions
        if abs(target_w - vid_w) <= 32 and abs(target_h - vid_h) <= 32:
            target_w, target_h = vid_w, vid_h
    elif img_ar >= 1.2:
        # Wide banner / landscape image
        target_w = int(vid_w * pct)
        target_h = int(target_w / img_ar)
        if target_h > int(vid_h * 0.9):
            target_h = int(vid_h * 0.9)
            target_w = int(target_h * img_ar)
    else:
        # Square or portrait / vertical image (e.g. logo, avatar, poster)
        ref_dim = min(vid_w, vid_h)
        target_h = int(ref_dim * pct)
        target_w = int(target_h * img_ar)
        if target_w > int(vid_w * 0.9):
            target_w = int(vid_w * 0.9)
            target_h = int(target_w / img_ar)

    target_w = max(32, (int(target_w) // 2) * 2)
    target_h = max(32, (int(target_h) // 2) * 2)
    return target_w, target_h

def parse_timing_duration(timing_str, default_sec=10.0):
    """Parse timing duration in seconds from values like start_10, 15, end_20, full, etc."""
    timing_str = str(timing_str or "").strip().lower()
    if timing_str in ("full", "all", "entire"):
        return None
    import re
    m = re.search(r'(\d+)', timing_str)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            pass
    return default_sec

def build_color_filter_str(preset, brightness="0.03", contrast="1.10", saturation="1.15", hue="4"):
    """
    Builds FFmpeg video filter for anti-copyright color alteration / grading.
    """
    if not preset or str(preset).strip().lower() in ("none", "off", "0", "false"):
        return ""
    
    p = str(preset).strip().lower()
    if p == "anti_copyright_mild":
        return "eq=contrast=1.07:brightness=0.03:saturation=1.14:gamma=1.02,hue=h=3"
    elif p == "anti_copyright_strong":
        return "eq=contrast=1.14:brightness=0.05:saturation=1.22:gamma=1.05,hue=h=6"
    elif p == "cinematic_warm":
        return "eq=contrast=1.10:brightness=0.02:saturation=1.12,colorbalance=rs=0.08:gs=0.02:bs=-0.06:rm=0.06:gm=0.01:bm=-0.05"
    elif p == "cinematic_cool":
        return "eq=contrast=1.12:brightness=0.02:saturation=1.10,colorbalance=rs=-0.05:bs=0.08:rm=-0.03:bm=0.06"
    elif p == "vibrant_hdr":
        return "eq=contrast=1.15:brightness=0.04:saturation=1.28:gamma=1.04"
    elif p == "vintage_film":
        return "eq=contrast=1.05:brightness=0.03:saturation=0.88:gamma=1.08,colorbalance=rs=0.06:gs=0.04:bs=-0.04"
    elif p == "black_white":
        return "hue=s=0,eq=contrast=1.18:brightness=0.02"
    elif p == "custom":
        try:
            b = float(brightness)
            b = max(-0.5, min(0.5, b))
        except Exception:
            b = 0.03
        try:
            c = float(contrast)
            c = max(0.5, min(2.0, c))
        except Exception:
            c = 1.10
        try:
            s = float(saturation)
            s = max(0.0, min(3.0, s))
        except Exception:
            s = 1.15
        try:
            h = float(hue)
            h = max(-180.0, min(180.0, h))
        except Exception:
            h = 4.0
        return f"eq=contrast={c:.2f}:brightness={b:.2f}:saturation={s:.2f},hue=h={h:.1f}"
    return ""

def create_ass_overlay(text, start_sec, end_sec, video_w, video_h,
                       font_name, font_size, color, pos,
                       stroke_color, stroke_width, output_path):
    return create_video_ass_file(
        output_path=output_path, video_w=video_w, video_h=video_h, video_dur_sec=end_sec,
        text_overlay=text, text_timing='full', text_pos=pos,
        text_size=font_size, text_font=font_name, text_color=color,
        text_stroke_color=stroke_color, text_stroke_width=stroke_width
    )
async def compile_single_dub_backend(video_path, srt_path, voice, orig_vol, tts_vol, vocal_removed, speed_rate, output_path, log_callback, auto_voice=False, time_offset_ms=0, blur_zones=None, mirror_video=False, task_id=None, fix_freeze=True, pitch="+10%", text_overlay="", text_timing="start_30", text_pos="bottom_center", text_size="32", text_font="KhmerOSniroth", text_color="white", text_stroke_color="black", text_stroke_width="2", outro_text="", outro_pos="bottom_center", outro_size="42", outro_font="KhmerOSniroth", outro_color="white", outro_stroke_color="black", outro_stroke_width="3", image_intro=None, image_intro_pos="center", image_intro_size="100", image_intro_opacity="1.0", image_intro_timing="start_10", image_outro=None, image_outro_pos="center", image_outro_size="100", image_outro_opacity="1.0", image_outro_timing="end_10", color_filter="none", color_brightness="0.03", color_contrast="1.10", color_saturation="1.15", color_hue="4"):

    temp_dir = f"temp_backend_dub_{uuid.uuid4()}"
    try:
        log_callback("Reading SRT file...")
        subtitles = parse_srt(srt_path)
        
        if not subtitles:
            log_callback("Error: No subtitles found in SRT file.")
            return False
            
        # Shift subtitles if absolute timecode offset is detected
        if time_offset_ms != 0:
            for sub in subtitles:
                sub['start_ms'] = max(0, sub['start_ms'] - time_offset_ms)
                sub['end_ms'] = max(0, sub['end_ms'] - time_offset_ms)
        
        os.makedirs(temp_dir, exist_ok=True)
        
        # Check system ffmpeg first
        import shutil
        if shutil.which("ffmpeg"):
            ffmpeg_exe = "ffmpeg"
        else:
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            
        # 1. Calculate overall duration safely (ensure subtitles near the end are never clipped)
        video_dur_sec = get_video_duration(video_path, ffmpeg_exe)
        max_sub_end = max((s['end_ms'] for s in subtitles), default=0)
        total_ms = max(int((video_dur_sec or 0) * 1000), max_sub_end + 3000)
        # Safety cap: never allocate more than 3 hours of PCM to prevent OOM on bad SRT files
        MAX_PCM_MS = 3 * 3600 * 1000  # 3 hours
        if total_ms > MAX_PCM_MS:
            log_callback(f"⚠️ PCM buffer capped at 3 hours (requested {total_ms/1000:.0f}s). Check SRT timestamps.")
            total_ms = MAX_PCM_MS
        
        # Each ms represents 48 bytes (24000Hz * 2 bytes/sample * 1 channel)
        bytes_per_ms = 48
        final_pcm = bytearray(total_ms * bytes_per_ms)
        
        temp_files_to_clean = []
        rate_param = f"{speed_rate:+d}%" if speed_rate != 0 else "+0%"

        # Normalize voice & pitch
        voice = normalize_voice_name(voice)
        default_pitch = normalize_edge_pitch(pitch) or "+15Hz"

        # Concurrency semaphore: up to 16 concurrent Edge-TTS requests for ultra-fast parallel dubbing
        sem = asyncio.Semaphore(16)
        completed_count = 0
        _count_lock = asyncio.Lock()
        total_subs = len(subtitles)

        async def process_subtitle(i, sub):
            nonlocal completed_count
            if task_id and active_tasks.get(task_id, {}).get('status') in ('failed', 'cancelled'):
                return i, sub, None

            temp_mp3 = os.path.join(temp_dir, f"temp_tts_{i}.mp3")
            seg_voice = voice
            seg_pitch = default_pitch
            sub_text = sub['text'].strip()

            if auto_voice:
                if re.search(r'[\(\[]\s*(?:female|ស្រី|woman|កញ្ញា|នារី)\s*[\)\]]', sub_text, re.IGNORECASE):
                    seg_voice = 'km-KH-SreymomNeural'
                    sub_text = re.sub(r'\s*[\(\[]\s*(?:female|ស្រី|woman|កញ្ញា|នារី)\s*[\)\]]', '', sub_text, flags=re.IGNORECASE).strip()
                elif re.search(r'[\(\[]\s*(?:male|ប្រុស|man|លោក|បុរស)\s*[\)\]]', sub_text, re.IGNORECASE):
                    seg_voice = 'km-KH-PisethNeural'
                    sub_text = re.sub(r'\s*[\(\[]\s*(?:male|ប្រុស|man|លោក|បុរស)\s*[\)\]]', '', sub_text, flags=re.IGNORECASE).strip()

                tag_match = re.search(r'<dubbing\s+[^>]*voice="([^"]+)"[^>]*>(.*?)</dubbing>', sub_text, re.DOTALL | re.IGNORECASE)
                if tag_match:
                    seg_voice_val = tag_match.group(1).strip()
                    sub_text = tag_match.group(2).strip()
                    seg_voice = normalize_voice_name(seg_voice_val)
                        
                    pitch_match = re.search(r'pitch="([^"]+)"', tag_match.group(0), re.IGNORECASE)
                    if pitch_match:
                        seg_pitch = normalize_edge_pitch(pitch_match.group(1).strip()) or default_pitch
            else:
                sub_text = re.sub(r'\s*[\(\[]\s*(?:female|ស្រី|woman|កញ្ញា|នារី|male|ប្រុស|man|លោក|បុរស)\s*[\)\]]', '', sub_text, flags=re.IGNORECASE).strip()
                sub_text = re.sub(r'<dubbing[^>]*>', '', sub_text, flags=re.IGNORECASE)
                sub_text = re.sub(r'</dubbing>', '', sub_text, flags=re.IGNORECASE)

            if not sub_text:
                completed_count += 1
                return i, sub, None

            async with sem:
                if task_id and active_tasks.get(task_id, {}).get('status') in ('failed', 'cancelled'):
                    return i, sub, None
                try:
                    engine_used = await generate_tts_mp3(sub_text, seg_voice, rate_param, temp_mp3, seg_pitch)
                    if engine_used != "edge-tts":
                        log_callback(f"  Info: Subtitle {i+1} used {engine_used} (edge-tts fallback)")
                except Exception as e:
                    completed_count += 1
                    log_callback(f"  Warning: All TTS engines failed for subtitle {i+1}: {e}")
                    return i, sub, None

            if not os.path.exists(temp_mp3) or os.path.getsize(temp_mp3) == 0:
                completed_count += 1
                return i, sub, None

            # Direct in-memory conversion from temp_mp3 to 24kHz s16le raw PCM (no disk WAV files)
            cmd = [
                ffmpeg_exe, "-y",
                "-i", temp_mp3,
                "-filter:a", "atrim=start_sample=1024,asetpts=PTS-STARTPTS,aresample=24000",
                "-ar", "24000",
                "-ac", "1",
                "-f", "s16le",
                "-"
            ]
            loop = asyncio.get_running_loop()
            transcode_result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            )
            try: os.remove(temp_mp3)
            except Exception: pass

            raw_pcm = transcode_result.stdout
            if transcode_result.returncode != 0 or not raw_pcm:
                completed_count += 1
                return i, sub, None

            chunk_duration_ms = len(raw_pcm) // bytes_per_ms
            if chunk_duration_ms <= 0:
                completed_count += 1
                return i, sub, None

            sub_dur = max(sub['end_ms'] - sub['start_ms'], 300)
            if i + 1 < len(subtitles):
                gap_to_next = max(200, subtitles[i + 1]['start_ms'] - sub['start_ms'] - 80)
                allowed_duration_ms = max(sub_dur, min(gap_to_next, sub_dur + 800))
            else:
                allowed_duration_ms = sub_dur + 3000

            if allowed_duration_ms > 100 and chunk_duration_ms > allowed_duration_ms * 1.05:
                tempo = min(1.95, chunk_duration_ms / allowed_duration_ms)
                speed_cmd = [
                    ffmpeg_exe, "-y",
                    "-f", "s16le", "-ar", "24000", "-ac", "1",
                    "-i", "-",
                    "-filter:a", f"atempo={tempo:.3f}",
                    "-f", "s16le",
                    "-"
                ]
                speed_result = await loop.run_in_executor(
                    None,
                    lambda: subprocess.run(speed_cmd, input=raw_pcm, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                )
                if speed_result.returncode == 0 and speed_result.stdout:
                    raw_pcm = speed_result.stdout

            async with _count_lock:
                completed_count += 1
                log_callback(f"Generating TTS for subtitle {completed_count}/{total_subs}...")
            return i, sub, raw_pcm

        tasks = [process_subtitle(i, sub) for i, sub in enumerate(subtitles)]
        results = await asyncio.gather(*tasks)

        if task_id and active_tasks.get(task_id, {}).get('status') in ('failed', 'cancelled'):
            log_callback("Operation cancelled.")
            return False

        import struct, math, array as _array

        # Pre-build cosine fade lookup table (60ms = 1440 samples at 24kHz)
        FADE_SAMPLES = 1440
        _fade_lut = _array.array('d', [
            (1.0 - math.cos(math.pi * fi / FADE_SAMPLES)) / 2.0
            for fi in range(FADE_SAMPLES)
        ])

        for i, sub, raw_pcm in sorted(results, key=lambda r: r[0]):
            if not raw_pcm:
                continue
            start_byte = sub['start_ms'] * bytes_per_ms
            if start_byte >= len(final_pcm):
                continue
            write_len = min(len(raw_pcm), len(final_pcm) - start_byte)
            write_len = (write_len // 2) * 2

            if write_len > 0:
                n_samples = write_len // 2
                new_s = _array.array('h', raw_pcm[:write_len])

                # Fast vectorized cosine fade-in using pre-built LUT
                fade_in_len = min(FADE_SAMPLES, n_samples)
                for fi in range(fade_in_len):
                    new_s[fi] = int(new_s[fi] * _fade_lut[fi])

                # Fast vectorized cosine fade-out using pre-built LUT
                fade_out_start = max(fade_in_len, n_samples - FADE_SAMPLES)
                for fo, idx in enumerate(range(fade_out_start, n_samples)):
                    lut_i = n_samples - 1 - idx
                    if 0 <= lut_i < FADE_SAMPLES:
                        new_s[idx] = int(new_s[idx] * _fade_lut[lut_i])

                # Mix with existing PCM using fast array arithmetic
                existing = _array.array('h', final_pcm[start_byte : start_byte + write_len])
                mixed = _array.array('h', (
                    max(-32768, min(32767, e + n))
                    for e, n in zip(existing, new_s)
                ))
                final_pcm[start_byte : start_byte + write_len] = mixed.tobytes()

        log_callback("Combining voice tracks...")
        tts_full_wav = os.path.join(temp_dir, "tts_full.wav")
        temp_files_to_clean.append(tts_full_wav)
        
        import struct
        num_samples = len(final_pcm) // 2
        num_channels = 1
        bits_per_sample = 16
        sample_rate = 24000
        byte_rate = sample_rate * num_channels * (bits_per_sample // 8)
        block_align = num_channels * (bits_per_sample // 8)
        data_size = num_samples * block_align
        file_size = 36 + data_size
        
        wav_header = struct.pack(
            '<4sI4s4sIHHIIHH4sI',
            b'RIFF', file_size, b'WAVE', b'fmt ', 16, 1, num_channels,
            sample_rate, byte_rate, block_align, bits_per_sample, b'data', data_size
        )
        
        with open(tts_full_wav, 'wb') as out_f:
            out_f.write(wav_header)
            out_f.write(final_pcm)

        # Check if original video has an audio stream
        has_audio = False
        try:
            cmd = [ffmpeg_exe, "-i", video_path]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            if "Audio:" in res.stderr:
                has_audio = True
        except Exception as e:
            print("Failed probing audio streams:", e)
            has_audio = True # Default fallback
            
        if not has_audio:
            log_callback("Original video has no audio stream. Mapping translator track directly.")
                
        # Audio Mix and Video Render
        log_callback("Merging audio into video stream...")
        orig_vol_ratio = orig_vol / 100.0
        tts_vol_ratio = tts_vol / 100.0
        
        if vocal_removed:
            orig_vol_ratio *= 0.15
            
        v_map = "0:v"
        v_filter_str = ""

        # Video encoding strategy
        # If fix_freeze, mirror_video, blur_zones, text_overlay, outro_text, image overlays, or color filter active -> Re-encode with safe H.264 CFR (30fps)
        has_overlay = bool(text_overlay and text_overlay.strip())
        has_outro = bool(outro_text and outro_text.strip())
        has_img_intro = bool(image_intro and os.path.exists(image_intro))
        has_img_outro = bool(image_outro and os.path.exists(image_outro))
        has_color_filter = bool(color_filter and str(color_filter).strip().lower() not in ('none', 'off', '0', 'false', ''))

        extra_img_inputs = []
        intro_input_idx = None
        outro_input_idx = None
        next_input_idx = 2  # 0 is video_path, 1 is tts_full_wav
        if has_img_intro:
            intro_input_idx = next_input_idx
            next_input_idx += 1
            extra_img_inputs.extend(["-loop", "1", "-i", image_intro])
        if has_img_outro:
            outro_input_idx = next_input_idx
            next_input_idx += 1
            extra_img_inputs.extend(["-loop", "1", "-i", image_outro])

        target_fps = get_video_fps(video_path, ffmpeg_exe)
        if fix_freeze or mirror_video or blur_zones or has_overlay or has_outro or has_img_intro or has_img_outro or has_color_filter:
            best_enc = get_best_h264_encoder(ffmpeg_exe)
            hw_name = "Intel QuickSync GPU" if "qsv" in best_enc[1] else ("NVIDIA NVENC GPU" if "nvenc" in best_enc[1] else "CPU Ultra-Fast")
            log_callback(f"🛡️ កំពុងដំណើរការ Safe Video Encoding (CFR {target_fps}fps) តាមរយៈ {hw_name} ដើម្បីធានាកុំឱ្យគាំងរូបភាព...")
            v_codec = best_enc
            curr_stream = "[v_clean]"
            vf_parts = [f"[0:v]setpts=PTS-STARTPTS,fps=fps={target_fps}:round=near[v_clean]"]
        else:
            v_codec = ["-c:v", "copy"]
            curr_stream = "[0:v]"
            vf_parts = []
            log_callback("⚡ រក្សាកូដវីដេអូដើម (Direct Stream Copy - លឿនរហ័ស 1-2 វិនាទី គុណភាពដើម 100%)...")

        if mirror_video or blur_zones or has_overlay or has_outro or has_img_intro or has_img_outro or has_color_filter:
            vid_w, vid_h = get_video_dimensions(video_path, ffmpeg_exe)

            if mirror_video:
                log_callback("🪞 កំពុងធ្វើ Mirror វីដេអូ (Flip Horizontal)...")
                next_stream = "[v_mirrored]"
                vf_parts.append(f"{curr_stream}hflip{next_stream}")
                curr_stream = next_stream

            if has_color_filter:
                color_vf = build_color_filter_str(color_filter, color_brightness, color_contrast, color_saturation, color_hue)
                if color_vf:
                    log_callback(f"🎨 កំពុងប្តូរពណ៌វីដេអូ / ផ្តាច់ Copyright (Preset: {color_filter})...")
                    next_stream = "[v_colored]"
                    vf_parts.append(f"{curr_stream}{color_vf}{next_stream}")
                    curr_stream = next_stream


            # ─── Image Overlay: Intro Image (ដើមវីដេអូ ៣០ វិនាទី) ───
            if has_img_intro and intro_input_idx is not None:
                try:
                    pct = float(image_intro_size) / 100.0
                except Exception:
                    pct = 0.25
                target_w, target_h = calc_scaled_image_dims(image_intro, vid_w, vid_h, pct)
                try:
                    op_val = float(image_intro_opacity)
                except Exception:
                    op_val = 1.0
                op_val = max(0.1, min(1.0, op_val))
                xy_coords = get_overlay_coords(image_intro_pos)
                intro_dur = parse_timing_duration(image_intro_timing, default_sec=30.0)
                if intro_dur is None:
                    enable_expr = "gte(t,0)"
                    timing_lbl = "ពេញមួយវីដេអូ"
                else:
                    dur_end = min(intro_dur, video_dur_sec) if video_dur_sec else intro_dur
                    enable_expr = f"between(t,0,{dur_end:.2f})"
                    timing_lbl = f"ដើមវីដេអូ {int(intro_dur)} វិនាទី"

                next_stream = "[v_img_intro]"
                vf_parts.append(f"[{intro_input_idx}:v]scale={target_w}:{target_h},format=rgba,colorchannelmixer=aa={op_val:.2f}[s_img_intro]")
                vf_parts.append(f"{curr_stream}[s_img_intro]overlay={xy_coords}:enable='{enable_expr}':shortest=1:eof_action=pass{next_stream}")
                curr_stream = next_stream
                log_callback(f"🖼️ បានបន្ថែមរូបភាពដើមវីដេអូ (Intro Image, {timing_lbl}, ទំហំ {int(pct*100)}% [{target_w}x{target_h}px])")

            # ─── Image Overlay: Outro Image ───
            if has_img_outro and outro_input_idx is not None:
                try:
                    pct = float(image_outro_size) / 100.0
                except Exception:
                    pct = 0.35
                target_w, target_h = calc_scaled_image_dims(image_outro, vid_w, vid_h, pct)
                try:
                    op_val = float(image_outro_opacity)
                except Exception:
                    op_val = 1.0
                op_val = max(0.1, min(1.0, op_val))
                xy_coords = get_overlay_coords(image_outro_pos)
                
                outro_dur = parse_timing_duration(image_outro_timing, default_sec=30.0)
                if outro_dur is None:
                    enable_expr = "gte(t,0)"
                    outro_lbl = "ពេញមួយវីដេអូ"
                else:
                    outro_start = max(0.0, video_dur_sec - outro_dur) if video_dur_sec else 0.0
                    dur_end = video_dur_sec if video_dur_sec else (outro_start + outro_dur)
                    enable_expr = f"between(t,{outro_start:.2f},{dur_end:.2f})"
                    outro_lbl = f"ចុងវីដេអូ {int(outro_dur)} វិនាទី"

                next_stream = "[v_img_outro]"
                vf_parts.append(f"[{outro_input_idx}:v]scale={target_w}:{target_h},format=rgba,colorchannelmixer=aa={op_val:.2f}[s_img_outro]")
                vf_parts.append(f"{curr_stream}[s_img_outro]overlay={xy_coords}:enable='{enable_expr}':shortest=1:eof_action=pass{next_stream}")
                curr_stream = next_stream
                log_callback(f"🎬 បានបន្ថែមរូបភាពចុងវីដេអូ (Outro Image, {outro_lbl}, ទំហំ {int(pct*100)}% [{target_w}x{target_h}px])")

            # ─── ASS Subtitles Overlay (HarfBuzz Khmer Shaping for correct ជើងអក្សរ & ស្រៈ) ───
            if has_overlay or has_outro:
                ass_file = os.path.join(temp_dir, f"overlay_{uuid.uuid4().hex[:8]}.ass")
                created_ass = create_video_ass_file(
                    output_path=ass_file,
                    video_w=vid_w,
                    video_h=vid_h,
                    video_dur_sec=video_dur_sec,
                    text_overlay=text_overlay,
                    text_timing=text_timing,
                    text_pos=text_pos,
                    text_size=text_size,
                    text_font=text_font,
                    text_color=text_color,
                    text_stroke_color=text_stroke_color,
                    text_stroke_width=text_stroke_width,
                    outro_text=outro_text,
                    outro_pos=outro_pos,
                    outro_size=outro_size,
                    outro_font=outro_font,
                    outro_color=outro_color,
                    outro_stroke_color=outro_stroke_color,
                    outro_stroke_width=outro_stroke_width,
                )
                if created_ass and os.path.exists(created_ass):
                    temp_files_to_clean.append(created_ass)
                    esc_ass = created_ass.replace('\\', '/').replace(':', r'\:')
                    next_stream = "[v_sub]"
                    vf_parts.append(f"{curr_stream}ass=filename='{esc_ass}'{next_stream}")
                    curr_stream = next_stream
                    if has_overlay:
                        timing_lbl = "ដើមវីដេអូ ៣០វិ" if text_timing == "start_30" else ("ចុងវីដេអូ ៣០វិ" if text_timing == "end_30" else ("ពេញមួយវីដេអូ" if text_timing == "full" else ("ដើម និង ចុង ៣០វិ" if text_timing == "start_end_30" else text_timing)))
                        log_callback(f"✍️ បានបន្ថែម Text Overlay ({timing_lbl}, HarfBuzz Khmer Shaping ជើងអក្សរភ្ជាប់ត្រឹមត្រូវ)")
                    if has_outro:
                        log_callback("🎬 បានបន្ថែម Outro Text (ចុងវីដេអូ ៣០វិ, HarfBuzz Khmer Shaping ជើងអក្សរភ្ជាប់ត្រឹមត្រូវ)")

            # ─── Blur Zones (applied LAST so it covers text/overlays too) ───
            if blur_zones:
                log_callback(f"🎨 កំពុងដាក់ Blur {len(blur_zones)} ចំណុចលើវីដេអូ ({vid_w}x{vid_h})...")
                print(f"[DEBUG] Processing {len(blur_zones)} blur zones: {blur_zones}")
                for i, z in enumerate(blur_zones):
                    try:
                        zx = int(min(z['x'], z['x'] + z.get('w', 0)) * vid_w)
                        zy = int(min(z['y'], z['y'] + z.get('h', 0)) * vid_h)
                        zw = int(abs(z.get('w', 0)) * vid_w)
                        zh = int(abs(z.get('h', 0)) * vid_h)

                        zw = min(vid_w, max(4, (zw // 2) * 2))
                        zh = min(vid_h, max(4, (zh // 2) * 2))
                        zx = max(0, min(vid_w - zw, (zx // 2) * 2))
                        zy = max(0, min(vid_h - zh, (zy // 2) * 2))

                        blur_strength = int(z.get('blur', 25))
                        # Scale sigma based on video width so 720p, 1080p, 4K all get strong, smooth frosted blur
                        # gblur (Gaussian Blur) has no radius <= min(w,h)/2 limitation and never fails with error -22 on small heights
                        scale_factor = max(0.5, vid_w / 720.0)
                        sigma = max(4.0, min(40.0, (blur_strength * 0.75) * scale_factor))
                        sigma_str = f"{sigma:.1f}"

                        next_stream = f"[vblur_{i}]"
                        vf_step = f"{curr_stream}split[vmain_{i}][vcrop_{i}]; [vcrop_{i}]crop={zw}:{zh}:{zx}:{zy},gblur=sigma={sigma_str}:steps=2[vblur_sub_{i}]; [vmain_{i}][vblur_sub_{i}]overlay={zx}:{zy}{next_stream}"
                        print(f"[DEBUG] Blur zone {i} (sigma={sigma_str}, zw={zw}, zh={zh}) vf_step: {vf_step}")
                        vf_parts.append(vf_step)
                        curr_stream = next_stream
                    except Exception as e:
                        print(f"[Server] Error processing blur zone {i}:", e)


            if vf_parts:
                v_filter_str = "; ".join(vf_parts)
                v_map = curr_stream
                v_codec = get_best_h264_encoder(ffmpeg_exe)
                print(f"[DEBUG] Final v_filter_str: {v_filter_str[:300]}")

        if has_audio:
            audio_filter = (
                f"[0:a]asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0:osr=44100,aformat=sample_rates=44100:channel_layouts=stereo,volume={orig_vol_ratio:.3f}[orig]; "
                f"[1:a]asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0:osr=44100,aformat=sample_rates=44100:channel_layouts=stereo,volume={tts_vol_ratio:.3f}[tts]; "
                f"[orig][tts]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
            )
        else:
            audio_filter = f"[1:a]asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0:osr=44100,aformat=sample_rates=44100:channel_layouts=stereo,volume={tts_vol_ratio:.3f}[aout]"

        filter_complex = audio_filter
        if v_filter_str:
            filter_complex = f"{v_filter_str}; {audio_filter}"

        def build_mux_cmd(chosen_codec, sanitize_pts=False):
            vf = filter_complex
            vm = v_map
            if sanitize_pts and chosen_codec != ["-c:v", "copy"]:
                # Normalize presentation timestamps to strictly monotonic 0-based time
                if v_filter_str:
                    if "setpts=PTS-STARTPTS" in v_filter_str:
                        vf = f"{v_filter_str}; {audio_filter}"
                    else:
                        vf = f"[0:v]setpts=PTS-STARTPTS[vclean]; {v_filter_str.replace('[0:v]', '[vclean]', 1)}; {audio_filter}"
                else:
                    vf = f"[0:v]setpts=PTS-STARTPTS[vclean]; {audio_filter}"
                    vm = "[vclean]"

            return [
                ffmpeg_exe, "-y",
                "-fflags", "+genpts+discardcorrupt",
                "-err_detect", "ignore_err",
                "-threads", "0",
                "-i", video_path,
                "-i", tts_full_wav,
            ] + extra_img_inputs + [
                "-filter_complex", vf,
                "-map", vm,
                "-map", "[aout]",
            ] + (["-t", f"{video_dur_sec:.3f}"] if (video_dur_sec and video_dur_sec > 0) else ["-shortest"]) + [
                "-max_muxing_queue_size", "4096"
            ] + chosen_codec + [
                "-c:a", "aac",
                "-b:a", "192k",
                "-ar", "44100",
                "-ac", "2",
                "-avoid_negative_ts", "make_zero",
                output_path
            ]

        sanitize = (v_codec != ["-c:v", "copy"])
        cmd = build_mux_cmd(v_codec, sanitize_pts=sanitize)
        dynamic_timeout = max(7200, int((video_dur_sec or 3600) * 5.0))

        # Real-time FFmpeg progress execution with monotonic countdown ETA
        retcode, err_peek = run_ffmpeg_with_progress(
            cmd, video_dur_sec, log_callback, task_id=task_id,
            base_pct=25.0, max_pct=97.0, timeout=dynamic_timeout, stage_name="Mix & Render"
        )
        is_success = (retcode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1024)
        
        # In rare cases where container rejects direct stream copy or hardware encoder fails,
        # smoothly fallback to universal CPU libx264 ultrafast encoder with sanitized monotonic PTS
        if not is_success:
            print(f"[Server] Video mux attempt 1 failed ({err_peek[-200:] if err_peek else ''}), switching to safe universal CPU re-encoding...")
            log_callback("⚙️ កំពុងដំណើរការ Safe Encoding ស្វ័យប្រវត្តិ (Universal CPU H.264 Ultra-Fast)...")
            
            # Clean up partial 0-byte or corrupted output file
            if os.path.exists(output_path):
                try: os.remove(output_path)
                except Exception: pass
                
            safe_cpu_enc = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-pix_fmt", "yuv420p", "-profile:v", "main", "-threads", "0"]
            fallback_cmd = build_mux_cmd(safe_cpu_enc, sanitize_pts=True)
            
            retcode, err_peek = run_ffmpeg_with_progress(
                fallback_cmd, video_dur_sec, log_callback, task_id=task_id,
                base_pct=25.0, max_pct=97.0, timeout=dynamic_timeout, stage_name="Re-encode Safe"
            )
            is_success = (retcode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1024)
        
        if is_success or (os.path.exists(output_path) and os.path.getsize(output_path) > 1024 * 1024):
            # Video was created successfully
            try_apply_faststart(output_path, ffmpeg_exe)
            return True
        else:
            raw_err = process.stderr.decode(errors='ignore') if process.stderr else "Unknown error"
            err_lines = [l.strip() for l in raw_err.splitlines() if any(k in l.lower() for k in ('error', 'invalid', 'failed', 'cannot', 'could not')) and 'terminating thread' not in l.lower()]
            err_msg = " | ".join(err_lines[-3:]) if err_lines else raw_err[-400:]
            print(f"[Server] Full FFmpeg stderr on failure:\n{raw_err[-1500:]}")
            log_callback(f"Error: FFmpeg video mux failed: {err_msg}")
            return False
        
    except asyncio.TimeoutError:
        log_callback("Fatal error: Operation timed out.")
        return False
    except Exception as e:
        log_callback(f"Compiler backend error: {e}")
        return False
    finally:
        # Always cleanup temp dir reliably using shutil.rmtree
        log_callback("Cleaning up temporary files...")
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


# ── Orchestrator: splits video when multiple SRT files provided ──────────────
async def compile_dubbed_video_backend(
    video_path, srt_paths_joined, voice, orig_vol, tts_vol,
    vocal_removed, speed_rate, output_path, log_callback, auto_voice=False, blur_zones=None, mirror_video=False, task_id=None, fix_freeze=True, pitch="+10%",
    text_overlay="", text_timing="start_30", text_pos="bottom_center", text_size="32", text_font="KhmerOSniroth", text_color="white", text_stroke_color="black", text_stroke_width="2",
    outro_text="", outro_pos="bottom_center", outro_size="42", outro_font="KhmerOSniroth", outro_color="white", outro_stroke_color="black", outro_stroke_width="3",
    image_intro=None, image_intro_pos="bottom_center", image_intro_size="25", image_intro_opacity="1.0", image_intro_timing="start_30",
    image_outro=None, image_outro_pos="bottom_center", image_outro_size="35", image_outro_opacity="1.0", image_outro_timing="end_30",
    color_filter="none", color_brightness="0.03", color_contrast="1.10", color_saturation="1.15", color_hue="4"
):
    import shutil as _shutil

    # Check system ffmpeg first
    if shutil.which("ffmpeg"):
        ffmpeg_exe = "ffmpeg"
    else:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    video_paths = [p.strip() for p in video_path.split(';') if p.strip()]
    srt_paths = [p.strip() for p in srt_paths_joined.split(';') if p.strip()]

    # ── MULTI-VIDEO BATCH MODE: dub each video file with matching SRT and concat ──
    if len(video_paths) > 1:
        num_vids = len(video_paths)
        log_callback(f"🎬 រកឃើញវីដេអូចំនួន {num_vids} និង SRT ចំនួន {len(srt_paths)} (Batch Mode)...")

        # CASE 1: Multiple videos with 1 unified SRT (standard output from Auto-Transcribe or single subtitle file)
        if len(srt_paths) <= 1:
            batch_temp_dir = f"temp_batch_srv_{uuid.uuid4()}"
            os.makedirs(batch_temp_dir, exist_ok=True)
            combined_video = os.path.join(batch_temp_dir, "combined_input.mp4")

            log_callback(f"⚡ វីដេអូច្រើន និង SRT សរុបតែមួយ — កំពុងភ្ជាប់វីដេអូទាំងអស់បញ្ចូលគ្នាមុន...")
            ok = robust_concat_videos(video_paths, combined_video, ffmpeg_exe, log_callback, fix_freeze=fix_freeze)
            if not ok:
                _shutil.rmtree(batch_temp_dir, ignore_errors=True)
                return False

            log_callback("✅ តភ្ជាប់វីដេអូជោគជ័យ! ចាប់ផ្ដើមបញ្ចូលសំឡេង dubbing តែម្ដង...")
            try:
                single_srt = srt_paths[0] if srt_paths else ""
                return await compile_single_dub_backend(
                    combined_video, single_srt, voice, orig_vol, tts_vol,
                    vocal_removed, speed_rate, output_path, log_callback, auto_voice,
                    blur_zones=blur_zones, mirror_video=mirror_video, task_id=task_id, fix_freeze=fix_freeze, pitch=pitch,
                    text_overlay=text_overlay, text_timing=text_timing, text_pos=text_pos, text_size=text_size,
                    text_font=text_font, text_color=text_color,
                    text_stroke_color=text_stroke_color, text_stroke_width=text_stroke_width,
                    outro_text=outro_text, outro_pos=outro_pos, outro_size=outro_size,
                    outro_font=outro_font, outro_color=outro_color,
                    outro_stroke_color=outro_stroke_color, outro_stroke_width=outro_stroke_width,
                    image_intro=image_intro, image_intro_pos=image_intro_pos, image_intro_size=image_intro_size, image_intro_opacity=image_intro_opacity, image_intro_timing=image_intro_timing,
                    image_outro=image_outro, image_outro_pos=image_outro_pos, image_outro_size=image_outro_size, image_outro_opacity=image_outro_opacity, image_outro_timing=image_outro_timing,
                    color_filter=color_filter, color_brightness=color_brightness, color_contrast=color_contrast, color_saturation=color_saturation, color_hue=color_hue
                )
            finally:
                _shutil.rmtree(batch_temp_dir, ignore_errors=True)

        # CASE 2: Multiple videos with Multiple SRTs (1 SRT per video)
        batch_temp_dir = f"temp_batch_srv_{uuid.uuid4()}"
        os.makedirs(batch_temp_dir, exist_ok=True)
        part_files = []
        base_out, ext_out = os.path.splitext(output_path)

        try:
            for idx, current_video in enumerate(video_paths):
                part_num = idx + 1
                current_srt = srt_paths[idx] if idx < len(srt_paths) else srt_paths[min(idx, len(srt_paths) - 1)]
                log_callback(f"\n📂 [វីដេអូទី {part_num}/{num_vids}] កំពុងដំណើរការ...")

                part_output = f"{base_out}_vid{part_num}{ext_out}"
                success = await compile_single_dub_backend(
                    current_video, current_srt, voice, orig_vol, tts_vol,
                    vocal_removed, speed_rate, part_output, log_callback, auto_voice,
                    blur_zones=blur_zones, mirror_video=mirror_video, task_id=task_id, fix_freeze=fix_freeze, pitch=pitch,
                    text_overlay=text_overlay, text_timing=text_timing, text_pos=text_pos, text_size=text_size,
                    text_font=text_font, text_color=text_color,
                    text_stroke_color=text_stroke_color, text_stroke_width=text_stroke_width,
                    outro_text=outro_text, outro_pos=outro_pos, outro_size=outro_size,
                    outro_font=outro_font, outro_color=outro_color,
                    outro_stroke_color=outro_stroke_color, outro_stroke_width=outro_stroke_width,
                    image_intro=image_intro, image_intro_pos=image_intro_pos, image_intro_size=image_intro_size, image_intro_opacity=image_intro_opacity, image_intro_timing=image_intro_timing,
                    image_outro=image_outro, image_outro_pos=image_outro_pos, image_outro_size=image_outro_size, image_outro_opacity=image_outro_opacity, image_outro_timing=image_outro_timing,
                    color_filter=color_filter, color_brightness=color_brightness, color_contrast=color_contrast, color_saturation=color_saturation, color_hue=color_hue
                )
                if not success:
                    log_callback(f"❌ បញ្ចូលសំឡេងវីដេអូទី {part_num} បរាជ័យ")
                    return False

                log_callback(f"✅ វីដេអូទី {part_num} រួចរាល់")
                part_files.append(part_output)

            # Concat all dubbed videos into final output using robust concat
            log_callback("\n🔗 កំពុងរួមបញ្ចូលវីដេអូទាំងអស់...")
            ok = robust_concat_videos(part_files, output_path, ffmpeg_exe, log_callback, fix_freeze=fix_freeze)
            if not ok:
                return False

            try_apply_faststart(output_path, ffmpeg_exe)
            log_callback(f"🎉 ជោគជ័យ! បង្កើតវីដេអូសរុប {num_vids} ផ្នែករួចរាល់")
            return True
        finally:
            _shutil.rmtree(batch_temp_dir, ignore_errors=True)
            for pf in part_files:
                try: os.remove(pf)
                except: pass

    # ── MULTI-SRT MODE: Merge all SRTs with smart timecode offsets and dub directly ──
    if len(srt_paths) > 1:
        total_duration = get_video_duration(video_path, ffmpeg_exe)
        num_parts = len(srt_paths)
        part_duration = (total_duration / num_parts) if total_duration else 0
        log_callback(
            f"🎬 រកឃើញ SRT ចំនួន {num_parts}។ "
            f"កំពុងច្របាច់បញ្ចូល SRT ទាំងអស់ចូលគ្នា (Fast Direct Dubbing មិនកាត់វីដេអូ)..."
        )

        merged_subs = []
        for idx, current_srt in enumerate(srt_paths):
            part_num = idx + 1
            temp_subs = parse_srt(current_srt)
            if not temp_subs:
                continue

            first_start = temp_subs[0]['start_ms']
            expected_start_ms = int(idx * part_duration * 1000)

            # If SRT starts at 0 (relative) for part > 1, shift it by expected_start_ms
            if idx > 0 and first_start < 5000 and expected_start_ms > 0:
                log_callback(f"⚙️ កែតម្រូវកូដម៉ោង SRT ទី {part_num} (+{expected_start_ms/1000:.1f}s)...")
                for sub in temp_subs:
                    sub['start_ms'] += expected_start_ms
                    sub['end_ms'] += expected_start_ms

            merged_subs.extend(temp_subs)

        # Sort all subtitles chronologically
        merged_subs.sort(key=lambda s: s['start_ms'])

        merged_temp_dir = f"temp_merged_srt_{uuid.uuid4()}"
        os.makedirs(merged_temp_dir, exist_ok=True)
        merged_srt_path = os.path.join(merged_temp_dir, "merged.srt")

        def _fmt_ts(ms):
            h = ms // 3600000
            m = (ms % 3600000) // 60000
            s = (ms % 60000) // 1000
            millis = ms % 1000
            return f"{h:02d}:{m:02d}:{s:02d},{millis:03d}"

        with open(merged_srt_path, 'w', encoding='utf-8') as f:
            for s_idx, s in enumerate(merged_subs):
                f.write(f"{s_idx + 1}\n{_fmt_ts(s['start_ms'])} --> {_fmt_ts(s['end_ms'])}\n{s['text']}\n\n")

        try:
            return await compile_single_dub_backend(
                video_path, merged_srt_path, voice, orig_vol, tts_vol,
                vocal_removed, speed_rate, output_path, log_callback, auto_voice,
                blur_zones=blur_zones, mirror_video=mirror_video, task_id=task_id, fix_freeze=fix_freeze, pitch=pitch,
                text_overlay=text_overlay, text_timing=text_timing, text_pos=text_pos, text_size=text_size,
                text_font=text_font, text_color=text_color,
                text_stroke_color=text_stroke_color, text_stroke_width=text_stroke_width,
                outro_text=outro_text, outro_pos=outro_pos, outro_size=outro_size,
                outro_font=outro_font, outro_color=outro_color,
                outro_stroke_color=outro_stroke_color, outro_stroke_width=outro_stroke_width,
                image_intro=image_intro, image_intro_pos=image_intro_pos, image_intro_size=image_intro_size, image_intro_opacity=image_intro_opacity, image_intro_timing=image_intro_timing,
                image_outro=image_outro, image_outro_pos=image_outro_pos, image_outro_size=image_outro_size, image_outro_opacity=image_outro_opacity, image_outro_timing=image_outro_timing,
                color_filter=color_filter, color_brightness=color_brightness, color_contrast=color_contrast, color_saturation=color_saturation, color_hue=color_hue
            )
        finally:
            _shutil.rmtree(merged_temp_dir, ignore_errors=True)

    # ── SINGLE SRT MODE (default or when only 1 SRT given) ──────────────────────
    return await compile_single_dub_backend(
        video_path, srt_paths[0], voice, orig_vol, tts_vol,
        vocal_removed, speed_rate, output_path, log_callback, auto_voice, blur_zones=blur_zones, mirror_video=mirror_video, task_id=task_id, fix_freeze=fix_freeze, pitch=pitch,
        text_overlay=text_overlay, text_timing=text_timing, text_pos=text_pos, text_size=text_size,
        text_font=text_font, text_color=text_color,
        text_stroke_color=text_stroke_color, text_stroke_width=text_stroke_width,
        outro_text=outro_text, outro_pos=outro_pos, outro_size=outro_size,
        outro_font=outro_font, outro_color=outro_color,
        outro_stroke_color=outro_stroke_color, outro_stroke_width=outro_stroke_width,
        image_intro=image_intro, image_intro_pos=image_intro_pos, image_intro_size=image_intro_size, image_intro_opacity=image_intro_opacity, image_intro_timing=image_intro_timing,
        image_outro=image_outro, image_outro_pos=image_outro_pos, image_outro_size=image_outro_size, image_outro_opacity=image_outro_opacity, image_outro_timing=image_outro_timing,
        color_filter=color_filter, color_brightness=color_brightness, color_contrast=color_contrast, color_saturation=color_saturation, color_hue=color_hue
    )


def start_compilation_thread(task_id, video_path, srt_paths_joined, voice, orig_vol, tts_vol, vocal_removed, speed_rate, output_path, auto_voice, orig_filename, blur_zones=None, mirror_video=False, fix_freeze=True, pitch="+10%", text_overlay="", text_timing="start_30", text_pos="bottom_center", text_size="32", text_font="KhmerOSniroth", text_color="white", text_stroke_color="black", text_stroke_width="2", outro_text="", outro_pos="bottom_center", outro_size="42", outro_font="KhmerOSniroth", outro_color="white", outro_stroke_color="black", outro_stroke_width="3", image_intro=None, image_intro_pos="center", image_intro_size="100", image_intro_opacity="1.0", image_intro_timing="start_10", image_outro=None, image_outro_pos="center", image_outro_size="100", image_outro_opacity="1.0", image_outro_timing="end_10", color_filter="none", color_brightness="0.03", color_contrast="1.10", color_saturation="1.15", color_hue="4"):
    loop = asyncio.new_event_loop()
    
    def log_callback(msg):
        update_task_progress(task_id, msg)
        
    coro = compile_dubbed_video_backend(
        video_path, srt_paths_joined, voice,
        orig_vol, tts_vol, vocal_removed, speed_rate,
        output_path, log_callback, auto_voice, blur_zones, mirror_video, task_id=task_id, fix_freeze=fix_freeze, pitch=pitch,
        text_overlay=text_overlay, text_timing=text_timing, text_pos=text_pos, text_size=text_size,
        text_font=text_font, text_color=text_color,
        text_stroke_color=text_stroke_color, text_stroke_width=text_stroke_width,
        outro_text=outro_text, outro_pos=outro_pos, outro_size=outro_size,
        outro_font=outro_font, outro_color=outro_color,
        outro_stroke_color=outro_stroke_color, outro_stroke_width=outro_stroke_width,
        image_intro=image_intro, image_intro_pos=image_intro_pos, image_intro_size=image_intro_size, image_intro_opacity=image_intro_opacity, image_intro_timing=image_intro_timing,
        image_outro=image_outro, image_outro_pos=image_outro_pos, image_outro_size=image_outro_size, image_outro_opacity=image_outro_opacity, image_outro_timing=image_outro_timing,
        color_filter=color_filter, color_brightness=color_brightness, color_contrast=color_contrast, color_saturation=color_saturation, color_hue=color_hue
    )
    
    def run():
        asyncio.set_event_loop(loop)
        task_temp_dir = active_tasks.get(task_id, {}).get('temp_dir')
        try:
            success = loop.run_until_complete(coro)
            if success and os.path.exists(output_path):
                # Copy to permanent location
                server_output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dubbed_outputs")
                os.makedirs(server_output_dir, exist_ok=True)
                
                import datetime
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                orig_name = "video"
                if orig_filename:
                    dot_idx = orig_filename.rfind('.')
                    if dot_idx != -1:
                        orig_name = orig_filename[:dot_idx]
                    else:
                        orig_name = orig_filename
                
                safe_orig_name = re.sub(r'[^a-zA-Z0-9_\u1780-\u17f9\-]', '_', orig_name)
                server_output_filename = f"{safe_orig_name}_dubbed_{timestamp}.mp4"
                server_output_file = os.path.join(server_output_dir, server_output_filename)
                
                shutil.copy2(output_path, server_output_file)
                
                with _tasks_lock:
                    active_tasks[task_id]['server_output_file'] = server_output_file
                    active_tasks[task_id]['output_video_path'] = server_output_file  # point to permanent copy
                    active_tasks[task_id]['progress_pct'] = 100
                    active_tasks[task_id]['status'] = 'completed'
                print(f"[Server Task {task_id}] Successfully finished. Output: {server_output_file}")
            else:
                with _tasks_lock:
                    active_tasks[task_id]['status'] = 'failed'
                    last_logs = "\n".join(active_tasks[task_id]['logs'][-8:]) if active_tasks[task_id]['logs'] else "No logs."
                    active_tasks[task_id]['error'] = f"Compilation failed. Log tail:\n{last_logs}"
                print(f"[Server Task {task_id}] Compilation returned False.")
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"[Server Task {task_id}] Exception:\n{tb}")
            with _tasks_lock:
                active_tasks[task_id]['status'] = 'failed'
                active_tasks[task_id]['error'] = f"Server error: {type(e).__name__}: {e}"
        finally:
            try:
                loop.close()
            except:
                pass
            # Clean up temporary upload directory reliably
            if task_temp_dir and os.path.isdir(task_temp_dir):
                try:
                    shutil.rmtree(task_temp_dir, ignore_errors=True)
                    print(f"[Server Task {task_id}] Cleaned up temp dir: {task_temp_dir}")
                except Exception as ce:
                    print(f"[Server Task {task_id}] Temp dir cleanup failed: {ce}")

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()


# Custom HTTP Handler class
class DubbingHandler(http.server.SimpleHTTPRequestHandler):
    timeout = 300  # 5 minutes socket timeout for large video uploads
    
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip('/')
        print(f"[Server Request] GET {path}")

        # --- /favicon.ico: Serve favicon ---
        if parsed_url.path == '/favicon.ico':
            base_dir = os.path.dirname(os.path.abspath(__file__))
            icon_path = os.path.join(base_dir, "favicon.ico")
            if not os.path.exists(icon_path):
                icon_path = os.path.join(base_dir, "app_icon.ico")
            if os.path.exists(icon_path):
                self.send_response(200)
                self.send_header('Content-Type', 'image/x-icon')
                self.send_header('Content-Length', str(os.path.getsize(icon_path)))
                self.end_headers()
                with open(icon_path, 'rb') as f:
                    self.wfile.write(f.read())
                return

        # --- /api/status: Polls progress and status of a dubbing task ---
        if path == '/api/status':
            query = urllib.parse.parse_qs(parsed_url.query)
            task_id = query.get('task_id', [''])[0]
            if not task_id:
                self.send_error(400, "Missing 'task_id' query parameter.")
                return
            if task_id not in active_tasks:
                self.send_error(404, f"Task {task_id} not found.")
                return
            
            task = active_tasks[task_id]
            now = time.time()
            start_t = task.get('start_time', now)
            last_up = task.get('last_update', now)
            elapsed_sec = max(0, round(now - start_t, 1))
            last_sec = max(0, round(now - last_up, 1))

            status_data = {
                "status": task['status'],
                "progress_pct": task['progress_pct'],
                "message": task['message'],
                "current_part": task.get('current_part'),
                "total_parts": task.get('total_parts'),
                "subtitles_done": task.get('subtitles_done'),
                "total_subtitles": task.get('total_subtitles'),
                "elapsed_seconds": elapsed_sec,
                "last_update_seconds_ago": last_sec,
                "is_alive": True,
                "recent_logs": task.get('logs', [])[-10:],
                "error": task['error'],
                "est_rem_sec": task.get('est_rem_sec'),
                "render_speed": task.get('render_speed'),
                "stage": task.get('stage', 'tts')
            }
            body = json.dumps(status_data, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # --- /api/download: Downloads completed dubbing task video ---
        if parsed_url.path == '/api/download':
            query = urllib.parse.parse_qs(parsed_url.query)
            task_id = query.get('task_id', [''])[0]
            if not task_id:
                self.send_error(400, "Missing 'task_id' query parameter.")
                return
            if task_id not in active_tasks:
                self.send_error(404, f"Task {task_id} not found.")
                return
            
            task = active_tasks[task_id]
            if task['status'] != 'completed':
                self.send_error(400, f"Task {task_id} is in status '{task['status']}' (not completed).")
                return
                
            filepath = task.get('output_video_path')
            server_output_file = task.get('server_output_file')
            if not filepath or not os.path.exists(filepath):
                if server_output_file and os.path.exists(server_output_file):
                    filepath = server_output_file
                else:
                    self.send_error(404, "Dubbed video file not found on server.")
                    return
                
            file_size = os.path.getsize(filepath)
            range_header = self.headers.get('Range')

            start = 0
            end = file_size - 1
            status_code = 200

            if range_header and range_header.startswith('bytes='):
                try:
                    ranges = range_header.split('=')[1].strip()
                    parts = ranges.split('-')
                    if parts[0]:
                        start = int(parts[0])
                    if len(parts) > 1 and parts[1]:
                        end = int(parts[1])
                    if start <= end and start < file_size:
                        end = min(end, file_size - 1)
                        status_code = 206
                    else:
                        self.send_error(416, "Requested Range Not Satisfiable")
                        return
                except Exception:
                    status_code = 200
                    start = 0
                    end = file_size - 1

            content_length = end - start + 1
            self.send_response(status_code)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Accept-Ranges', 'bytes')
            if status_code == 206:
                self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
            self.send_header('Content-Length', str(content_length))
            if server_output_file:
                self.send_header('X-Output-Path', urllib.parse.quote(server_output_file))
                self.send_header('Access-Control-Expose-Headers', 'X-Output-Path')
            self.end_headers()
            
            try:
                with open(filepath, 'rb') as f:
                    f.seek(start)
                    remaining = content_length
                    chunk_size = 64 * 1024
                    while remaining > 0:
                        to_read = min(remaining, chunk_size)
                        buf = f.read(to_read)
                        if not buf:
                            break
                        self.wfile.write(buf)
                        remaining -= len(buf)
            except Exception:
                # Client disconnects during streaming or scrubbing are normal on iOS Safari
                pass
            return

        # --- /api/engine-status: Returns current TTS engine and availability ---
        if parsed_url.path == '/api/engine-status':
            status = {
                "edge_tts_available": True,  # Always importable; network may fail at runtime
                "gtts_available": GTTS_AVAILABLE,
                "active_engine": active_tts_engine,
                "voices": {
                    "Piseth":  "km-KH-PisethNeural  (Edge TTS - Male)",
                    "Sreymom": "km-KH-SreymomNeural (Edge TTS - Female)",
                    "Sokha":   "km-KH-SreymomNeural (Edge TTS - Female / alias)",
                    "Chitra":  "km-KH-PisethNeural  (Edge TTS - Male  / alias)"
                }
            }
            body = json.dumps(status, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # --- /api/server-info: Returns LAN IP, port and full URL for mobile QR ---
        if parsed_url.path == '/api/server-info':
            local_ip = get_local_ip()
            info = {
                "local_ip": local_ip,
                "port": PORT,
                "lan_url": f"http://{local_ip}:{PORT}/index.html",
                "localhost_url": f"http://localhost:{PORT}/index.html"
            }
            body = json.dumps(info, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # --- /api/test-voice: Quickly tests if edge-tts can reach Microsoft servers ---
        if parsed_url.path == '/api/test-voice':
            test_text = "សួស្តី"
            test_file = f"test_voice_{os.getpid()}.mp3"
            result = {"edge_tts": False, "gtts": False, "recommended": "gtts"}
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                engine = loop.run_until_complete(
                    generate_tts_mp3(test_text, "km-KH-PisethNeural", "+0%", test_file)
                )
                result["edge_tts"] = (engine == "edge-tts")
                result["gtts"] = (engine == "gtts") or GTTS_AVAILABLE
                result["recommended"] = engine
                result["message"] = f"TTS test successful using: {engine}"
            except Exception as e:
                result["message"] = f"Both TTS engines failed: {e}"
            finally:
                loop.close()
                try: os.remove(test_file)
                except: pass
            body = json.dumps(result, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # --- /api/tts: Real-time TTS preview (edge-tts → gTTS fallback) ---
        if parsed_url.path == '/api/tts':
            query = urllib.parse.parse_qs(parsed_url.query)
            text = query.get('text', [''])[0]
            voice_param = query.get('voice', ['Piseth'])[0]
            pitch_raw = query.get('pitch', ['+10%'])[0]
            pitch_param = normalize_edge_pitch(pitch_raw) or "+15Hz"

            if not text:
                self.send_error(400, "Missing 'text' query parameter.")
                return

            voice_map = {
                'Piseth':  'km-KH-PisethNeural',
                'Sreymom': 'km-KH-SreymomNeural',
                'Sokha':   'km-KH-SreymomNeural',
                'Chitra':  'km-KH-PisethNeural'
            }
            # Handle full voice name keys passed directly (from auto-detected tags)
            if voice_param in voice_map.values():
                edge_voice = voice_param
            else:
                edge_voice = voice_map.get(voice_param, 'km-KH-SreymomNeural')
                
            print(f"[TTS Preview] voice={edge_voice} pitch={pitch_param} len={len(text)}")

            temp_file = f"temp_tts_{os.getpid()}_{uuid.uuid4().hex[:12]}.mp3"
            loop = asyncio.new_event_loop()
            engine_used = "unknown"
            try:
                asyncio.set_event_loop(loop)
                engine_used = loop.run_until_complete(
                    generate_tts_mp3(text, edge_voice, "+0%", temp_file, pitch_param)
                )
                print(f"[TTS Preview] engine={engine_used}")
            except Exception as e:
                print(f"[TTS Preview] All engines failed: {e}")
                self.send_error(500, f"TTS generation failed: {e}")
                return
            finally:
                loop.close()

            if os.path.exists(temp_file) and os.path.getsize(temp_file) > 0:
                file_size = os.path.getsize(temp_file)
                self.send_response(200)
                self.send_header('Content-Type', 'audio/mpeg')
                self.send_header('Content-Length', str(file_size))
                self.send_header('X-TTS-Engine', engine_used)  # Let browser know which engine
                self.end_headers()
                with open(temp_file, 'rb') as f:
                    self.wfile.write(f.read())
                try: os.remove(temp_file)
                except: pass
            else:
                self.send_error(500, "Preview audio output file not found.")
            return

        super().do_GET()


    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip('/')
        print(f"[Server Request] POST {path}")
        
        # Route cancel API
        if path == '/api/cancel':
            query = urllib.parse.parse_qs(parsed_url.query)
            task_id = query.get('task_id', [''])[0]
            if task_id in active_tasks:
                active_tasks[task_id]['status'] = 'failed'
                active_tasks[task_id]['error'] = 'Task was cancelled by user.'
                active_tasks[task_id]['message'] = 'បោះបង់ដោយអ្នកប្រើប្រាស់'
                print(f"[Server Task {task_id}] Cancelled by user request.")
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'{"success": true}')
            return

        # Route the real video dubbing API
        if path == '/api/dub':
            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in content_type:
                self.send_error(400, "Content-Type must be multipart/form-data")
                return
                
            boundary_match = re.search(r'boundary=([^;]+)', content_type)
            if not boundary_match:
                self.send_error(400, "Missing boundary in Content-Type")
                return
            boundary = boundary_match.group(1).strip().strip('"')
            
            # Read post body bytes safely in 4MB chunks
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                remaining = content_length
                chunk_size = 1024 * 1024 * 4
                chunks = []
                while remaining > 0:
                    read_size = min(remaining, chunk_size)
                    chunk = self.rfile.read(read_size)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                body_bytes = b''.join(chunks)
            else:
                body_bytes = b''
            
            # Parse form fields and files
            form_data, files = parse_multipart(body_bytes, boundary)
            
            # Extract variables
            voice_param = form_data.get('voice', 'Sreymom')
            orig_vol = int(form_data.get('orig_vol', '0'))
            tts_vol = int(form_data.get('tts_vol', '100'))
            vocal_removed = form_data.get('vocal_removed', 'false').lower() == 'true'
            auto_voice = form_data.get('auto_voice', 'false').lower() == 'true'
            mirror_video = form_data.get('mirror_video', 'false').lower() == 'true'
            fix_freeze = form_data.get('fix_freeze', 'true').lower() == 'true'
            pitch = form_data.get('pitch', '+10%').strip() or '+10%'
            text_overlay = form_data.get('text_overlay', '').strip()
            text_timing = form_data.get('text_timing', 'start_30').strip()
            text_pos = form_data.get('text_pos', 'bottom_center').strip()
            text_size = form_data.get('text_size', '32').strip()
            text_font = form_data.get('text_font', 'KhmerOSniroth').strip()
            text_color = form_data.get('text_color', 'white').strip()
            text_stroke_color = form_data.get('text_stroke_color', 'black').strip()
            text_stroke_width = form_data.get('text_stroke_width', '2').strip()
            outro_text = form_data.get('outro_text', '').strip()
            outro_pos = form_data.get('outro_pos', 'bottom_center').strip()
            outro_size = form_data.get('outro_size', '42').strip()
            outro_font = form_data.get('outro_font', 'KhmerOSniroth').strip()
            outro_color = form_data.get('outro_color', 'white').strip()
            outro_stroke_color = form_data.get('outro_stroke_color', 'black').strip()
            outro_stroke_width = form_data.get('outro_stroke_width', '3').strip()

            image_intro_pos = form_data.get('image_intro_pos', 'center').strip()
            image_intro_size = form_data.get('image_intro_size', '100').strip()
            image_intro_opacity = form_data.get('image_intro_opacity', '1.0').strip()
            image_intro_timing = form_data.get('image_intro_timing', 'start_10').strip()

            image_outro_pos = form_data.get('image_outro_pos', 'center').strip()
            image_outro_size = form_data.get('image_outro_size', '100').strip()
            image_outro_opacity = form_data.get('image_outro_opacity', '1.0').strip()
            image_outro_timing = form_data.get('image_outro_timing', 'end_10').strip()
            
            color_filter = form_data.get('color_filter', 'none').strip()
            color_brightness = form_data.get('color_brightness', '0.03').strip()
            color_contrast = form_data.get('color_contrast', '1.10').strip()
            color_saturation = form_data.get('color_saturation', '1.15').strip()
            color_hue = form_data.get('color_hue', '4').strip()

            blur_zones_raw = form_data.get('blur_zones', '[]')
            try:
                blur_zones = json.loads(blur_zones_raw)
            except Exception:
                blur_zones = []
            print(f"[DEBUG] blur_zones received: {blur_zones_raw!r} → parsed: {blur_zones}")


            # Calculate speed rate float to percentage change
            speed_val = form_data.get('speed_rate', '1.0')
            speed_rate = 0
            try:
                rate_float = float(speed_val)
                if rate_float != 1.0:
                    speed_rate = int((rate_float - 1.0) * 100)
            except (ValueError, TypeError):
                pass
            
            # Choose Edge TTS voice ID
            voice_map = {
                'Piseth': 'km-KH-PisethNeural',
                'Sreymom': 'km-KH-SreymomNeural',
                'Sokha': 'km-KH-SreymomNeural',
                'Chitra': 'km-KH-PisethNeural'
            }
            voice = voice_map.get(voice_param, 'km-KH-SreymomNeural')
            
            # Setup sandbox directories
            temp_dir = f"temp_srv_dub_{uuid.uuid4()}"
            os.makedirs(temp_dir, exist_ok=True)

            input_video_path = os.path.join(temp_dir, "input_video.mp4")
            output_video_path = os.path.join(temp_dir, "output_dubbed.mp4")

            # ── Handle video: support single or multiple uploads (video, video_0, video_1, …) ──
            video_file_paths = []

            # Primary key 'video'
            if 'video' in files:
                v_save_path = os.path.join(temp_dir, "input_video_0.mp4")
                with open(v_save_path, 'wb') as f:
                    f.write(files['video']['content'])
                video_file_paths.append(v_save_path)

            # Additional video keys: video_0, video_1, video_2, …
            v_idx = 0
            while True:
                v_key = f"video_{v_idx}"
                if v_key not in files:
                    break
                v_save_path = os.path.join(temp_dir, f"input_video_extra_{v_idx}.mp4")
                with open(v_save_path, 'wb') as f:
                    f.write(files[v_key]['content'])
                if not (v_idx == 0 and 'video' in files):
                    video_file_paths.append(v_save_path)
                v_idx += 1

            if video_file_paths:
                input_video_path = ";".join(video_file_paths)
                print(f"[Server Dubbing] Video files ({len(video_file_paths)}): {input_video_path}")
            elif form_data.get('video_url'):
                input_video_path = os.path.join(temp_dir, "input_video.mp4")
                video_url = form_data['video_url'].strip()
                print(f"[Server Dubbing] Downloading video from URL: {video_url}...")
                try:
                    req = urllib.request.Request(
                        video_url,
                        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                    )
                    with urllib.request.urlopen(req, timeout=180) as response, open(input_video_path, 'wb') as out_file:
                        shutil.copyfileobj(response, out_file)
                    print("[Server Dubbing] Video downloaded successfully.")
                except Exception as e:
                    print("[Server Dubbing] Failed downloading video from URL:", e)
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    self.send_error(500, f"Failed to download video from URL: {e}")
                    return
            else:
                input_video_path = os.path.join(temp_dir, "input_video.mp4")
                demo_cache_path = "demo_video_cache.mp4"
                if not os.path.exists(demo_cache_path):
                    print("[Server Dubbing] Downloading sample video for Demo Mode cache...")
                    try:
                        req = urllib.request.Request(
                            "https://www.w3schools.com/html/mov_bbb.mp4",
                            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                        )
                        with urllib.request.urlopen(req, timeout=180) as response, open(demo_cache_path, 'wb') as out_file:
                            shutil.copyfileobj(response, out_file)
                    except Exception as e:
                        print("[Server Dubbing] Failed downloading demo video cache:", e)
                        shutil.rmtree(temp_dir, ignore_errors=True)
                        self.send_error(500, f"Demo file download failed: {e}")
                        return
                shutil.copy(demo_cache_path, input_video_path)

            # ── Handle SRT: support multiple uploads (srt, srt_0, srt_1, …) ──
            # Collect all SRT file keys in upload order
            srt_file_paths = []

            # Primary key 'srt' (single SRT or first SRT)
            if 'srt' in files:
                srt_save_path = os.path.join(temp_dir, "input_srt_0.srt")
                with open(srt_save_path, 'wb') as f:
                    f.write(files['srt']['content'])
                srt_file_paths.append(srt_save_path)

            # Additional SRT keys: srt_0, srt_1, srt_2, …
            idx = 0
            while True:
                key = f"srt_{idx}"
                if key not in files:
                    break
                srt_save_path = os.path.join(temp_dir, f"input_srt_extra_{idx}.srt")
                with open(srt_save_path, 'wb') as f:
                    f.write(files[key]['content'])
                # Only append if not already added via 'srt' key
                if not (idx == 0 and 'srt' in files):
                    srt_file_paths.append(srt_save_path)
                idx += 1

            if not srt_file_paths:
                shutil.rmtree(temp_dir, ignore_errors=True)
                self.send_error(400, "Missing SRT subtitles payload")
                return

            # Join all SRT paths with semicolon for the backend orchestrator
            input_srt_joined = ";".join(srt_file_paths)
            print(f"[Server Dubbing] SRT files ({len(srt_file_paths)}): {input_srt_joined}")
                
            # Generate task_id and register it in active_tasks
            task_id = str(uuid.uuid4())
            now_t = time.time()
            active_tasks[task_id] = {
                'status': 'running',
                'progress_pct': 0,
                'message': 'Starting compilation...',
                'logs': [],
                'start_time': now_t,
                'last_update': now_t,
                'server_output_file': None,
                'output_video_path': None,
                'temp_dir': temp_dir,
                'error': None
            }

            orig_filename = None
            if 'video' in files:
                orig_filename = files['video']['filename']

            image_intro_path = None
            if 'image_intro' in files:
                orig_name = files['image_intro'].get('filename', 'intro.png')
                ext = os.path.splitext(orig_name)[1] or '.png'
                raw_intro = os.path.join(temp_dir, f"image_intro_raw{ext}")
                with open(raw_intro, 'wb') as img_f:
                    img_f.write(files['image_intro']['content'])
                try:
                    from PIL import Image, ImageOps
                    with Image.open(raw_intro) as im:
                        im = ImageOps.exif_transpose(im)
                        if im.width > 2560 or im.height > 2560:
                            im.thumbnail((2560, 2560), Image.Resampling.LANCZOS)
                        image_intro_path = os.path.join(temp_dir, "image_intro_norm.png")
                        im.convert("RGBA").save(image_intro_path, format="PNG")
                except Exception as e_pil:
                    print(f"[Server Dubbing] PIL intro normalize warning: {e_pil}")
                    image_intro_path = raw_intro
                print(f"[Server Dubbing] ✅ ទទួលបានរូបភាពដើមវីដេអូ (image_intro): {image_intro_path} ({len(files['image_intro']['content'])} bytes)")

            image_outro_path = None
            if 'image_outro' in files:
                orig_name = files['image_outro'].get('filename', 'outro.png')
                ext = os.path.splitext(orig_name)[1] or '.png'
                raw_outro = os.path.join(temp_dir, f"image_outro_raw{ext}")
                with open(raw_outro, 'wb') as img_f:
                    img_f.write(files['image_outro']['content'])
                try:
                    from PIL import Image, ImageOps
                    with Image.open(raw_outro) as im:
                        im = ImageOps.exif_transpose(im)
                        if im.width > 2560 or im.height > 2560:
                            im.thumbnail((2560, 2560), Image.Resampling.LANCZOS)
                        image_outro_path = os.path.join(temp_dir, "image_outro_norm.png")
                        im.convert("RGBA").save(image_outro_path, format="PNG")
                except Exception as e_pil:
                    print(f"[Server Dubbing] PIL outro normalize warning: {e_pil}")
                    image_outro_path = raw_outro
                print(f"[Server Dubbing] ✅ ទទួលបានរូបភាពចុងវីដេអូ (image_outro): {image_outro_path} ({len(files['image_outro']['content'])} bytes)")

            start_compilation_thread(
                task_id, input_video_path, input_srt_joined, voice,
                orig_vol, tts_vol, vocal_removed, speed_rate,
                output_video_path, auto_voice, orig_filename, blur_zones, mirror_video, fix_freeze, pitch,
                text_overlay=text_overlay, text_timing=text_timing, text_pos=text_pos, text_size=text_size,
                text_font=text_font, text_color=text_color,
                text_stroke_color=text_stroke_color, text_stroke_width=text_stroke_width,
                outro_text=outro_text, outro_pos=outro_pos, outro_size=outro_size,
                outro_font=outro_font, outro_color=outro_color,
                outro_stroke_color=outro_stroke_color, outro_stroke_width=outro_stroke_width,
                image_intro=image_intro_path,
                image_intro_pos=image_intro_pos,
                image_intro_size=image_intro_size,
                image_intro_opacity=image_intro_opacity,
                image_intro_timing=image_intro_timing,
                image_outro=image_outro_path,
                image_outro_pos=image_outro_pos,
                image_outro_size=image_outro_size,
                image_outro_opacity=image_outro_opacity,
                image_outro_timing=image_outro_timing,
                color_filter=color_filter,
                color_brightness=color_brightness,
                color_contrast=color_contrast,
                color_saturation=color_saturation,
                color_hue=color_hue
            )

            # Return task_id immediately
            response_data = {"task_id": task_id}
            body = json.dumps(response_data).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Route the translate API
        elif path == '/api/translate':
            content_length = int(self.headers.get('Content-Length', 0))
            body_bytes = self.rfile.read(content_length)
            try:
                data = json.loads(body_bytes.decode('utf-8'))
                srt_content = data.get('srt', '')
                gemini_key = data.get('gemini_key', '').strip()
                if not gemini_key:
                    key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini_key.txt")
                    if os.path.exists(key_file):
                        try:
                            with open(key_file, "r", encoding="utf-8") as kf:
                                gemini_key = kf.read().strip()
                        except Exception:
                            pass

                if not srt_content:
                    self.send_error(400, "Missing srt content")
                    return
                
                blocks = srt_content.strip().split('\n\n')
                parsed_blocks = []
                for idx_b, block in enumerate(blocks):
                    lines = block.split('\n')
                    if len(lines) >= 3:
                        idx = lines[0]
                        time_line = lines[1]
                        text = "\n".join(lines[2:])
                        tag = ""
                        clean_text = text.strip()
                        if clean_text.lower().endswith("(female)"):
                            tag = " (Female)"
                            clean_text = clean_text[:-8].strip()
                        elif clean_text.lower().endswith("(male)"):
                            tag = " (Male)"
                            clean_text = clean_text[:-6].strip()
                        parsed_blocks.append({"idx": idx, "time_line": time_line, "clean_text": clean_text, "tag": tag, "valid": True})
                    else:
                        parsed_blocks.append({"block": block, "valid": False})
                
                original_lines = [b["clean_text"] for b in parsed_blocks if b["valid"] and b["clean_text"]]
                translated_map = {}
                
                # ── Gemini Translation (high quality) ──────────────────────────
                if gemini_key and original_lines:
                    try:
                        srt_payload = ""
                        for i, b in enumerate([b for b in parsed_blocks if b["valid"] and b["clean_text"]]):
                            m = i // 60
                            s = i % 60
                            srt_payload += f"{i+1}\n00:{m:02d}:{s:02d},000 --> 00:{m:02d}:{s:02d},999\n{b['clean_text']}\n\n"
                        
                        prompt = (
                            "You are a professional movie script writer and translator specializing in Khmer movie dubbing (បញ្ចូលសំឡេងភាពយន្ត).\n"
                            "Translate the following subtitles into natural, conversational, and contextually fluent Khmer (ភាសាខ្មែរ).\n\n"
                            "Requirements:\n"
                            "1. Cinematic Tone: Make the Khmer translation sound like real spoken dialogues in a movie. Avoid formal or robotic translations.\n"
                            "2. Appropriate Pronouns: Choose matching Khmer pronouns (e.g., ខ្ញុំ, បង, អូន, ឯង, លោក, ម៉ាក់, ប៉ា) based on the context so the characters sound natural speaking to each other.\n"
                            "3. Idiom Translation: Translate idioms, slang, and casual phrases into their natural Khmer equivalents rather than word-for-word.\n"
                            "4. Strict Format:\n"
                            "   - Translate ONLY the subtitle text lines.\n"
                            "   - Keep all SRT index numbers and timestamps EXACTLY as they are.\n"
                            "   - Return ONLY the clean, raw translated SRT content. Do NOT include any intro, notes, or code blocks.\n\n"
                            f"SRT subtitles to translate:\n{srt_payload}"
                        )
                        
                        models_to_try = [
                            "gemini-2.5-flash-preview-05-20",
                            "gemini-2.5-flash",
                            "gemini-2.0-flash",
                            "gemini-2.5-flash-lite",
                            "gemini-flash-latest",
                            "gemini-1.5-flash",
                            "gemini-2.5-pro",
                            "gemini-1.5-pro"
                        ]
                        translated_srt_text = None
                        gemini_payload = json.dumps({
                            "contents": [{"parts": [{"text": prompt}]}],
                            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192}
                        }).encode('utf-8')

                        for model_name in models_to_try:
                            try:
                                gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={gemini_key}"
                                gemini_req = urllib.request.Request(
                                    gemini_url,
                                    data=gemini_payload,
                                    headers={"Content-Type": "application/json"},
                                    method="POST"
                                )
                                with urllib.request.urlopen(gemini_req, timeout=60) as gemini_resp:
                                    gemini_result = json.loads(gemini_resp.read().decode('utf-8'))
                                    translated_srt_text = gemini_result['candidates'][0]['content']['parts'][0]['text'].strip()
                                    if translated_srt_text:
                                        print(f"[Server Translate] Gemini translation successful with model: {model_name}")
                                        break
                            except Exception as m_err:
                                print(f"[Server Translate] Gemini model {model_name} failed: {m_err}")

                        if not translated_srt_text:
                            raise ValueError("All Gemini translation models failed.")
                        
                        gemini_blocks = translated_srt_text.strip().split('\n\n')
                        translated_lines = []
                        for blk in gemini_blocks:
                            blk_lines = blk.strip().split('\n')
                            if len(blk_lines) >= 3:
                                translated_lines.append('\n'.join(blk_lines[2:]).strip())
                            elif len(blk_lines) == 2 and '-->' in blk_lines[1]:
                                translated_lines.append('')
                        
                        if len(translated_lines) >= int(len(original_lines) * 0.7):
                            line_idx = 0
                            for b in parsed_blocks:
                                if b["valid"] and b["clean_text"]:
                                    if line_idx < len(translated_lines):
                                        translated_map[b["clean_text"]] = translated_lines[line_idx]
                                    line_idx += 1
                            print(f"[Server Translate] Gemini translation successful! ({len(translated_lines)} lines mapped to {len(original_lines)} original)")
                        else:
                            raise ValueError(f"Gemini line count too low: {len(translated_lines)} vs {len(original_lines)}")
                    except Exception as gemini_err:
                        print(f"[Server Translate] Gemini failed: {gemini_err}. Falling back to Google Translate...")
                        translated_map = {}

                # ── Google Translate fallback ───────────────────────────────────
                if not translated_map and original_lines:
                    payload = "\n".join(original_lines)
                    try:
                        # Try high-quality Neural mobile Google Translate endpoint first
                        full_translation = None
                        try:
                            url = "https://translate.google.com/m?sl=auto&tl=km&q=" + urllib.parse.quote(payload)
                            req = urllib.request.Request(url, headers={
                                'User-Agent': 'Mozilla/5.0 (Linux; Android 10; SM-A205U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36'
                            })
                            with urllib.request.urlopen(req, timeout=15) as response:
                                html_content = response.read().decode('utf-8')
                                match = re.search(r'class="result-container">([^<]+)', html_content)
                                if not match:
                                    match = re.search(r'class="t0">([^<]+)', html_content)
                                if match:
                                    import html as html_parser
                                    full_translation = html_parser.unescape(match.group(1).strip())
                        except Exception as premium_err:
                            print("[Server Translate] Premium batch translation failed:", premium_err)

                        if not full_translation:
                            # Fallback legacy gtx
                            url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=km&dt=t&q=" + urllib.parse.quote(payload)
                            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                            with urllib.request.urlopen(req, timeout=15) as response:
                                res_data = response.read().decode('utf-8')
                                parsed_json = json.loads(res_data)
                                full_translation = "".join(item[0] for item in parsed_json[0] if item[0])
                        
                        translated_lines = [line.strip() for line in full_translation.split('\n')]
                        
                        while len(translated_lines) > len(original_lines):
                            if not translated_lines[-1]:
                                translated_lines.pop()
                            else:
                                break
                                
                        if len(translated_lines) == len(original_lines):
                            line_idx = 0
                            for b in parsed_blocks:
                                if b["valid"] and b["clean_text"]:
                                    translated_map[b["clean_text"]] = translated_lines[line_idx]
                                    line_idx += 1
                        else:
                            raise ValueError("Line count mismatch in batch translation")
                    except Exception as e:
                        print("[Server Translate] Batch failed, falling back to line-by-line:", e)
                        for b in parsed_blocks:
                            if b["valid"] and b["clean_text"]:
                                clean_text = b["clean_text"]
                                translated_text = None
                                
                                # Try premium first
                                try:
                                    url = "https://translate.google.com/m?sl=auto&tl=km&q=" + urllib.parse.quote(clean_text)
                                    req = urllib.request.Request(url, headers={
                                        'User-Agent': 'Mozilla/5.0 (Linux; Android 10; SM-A205U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36'
                                    })
                                    with urllib.request.urlopen(req, timeout=8) as response:
                                        html_content = response.read().decode('utf-8')
                                        match = re.search(r'class="result-container">([^<]+)', html_content)
                                        if not match:
                                            match = re.search(r'class="t0">([^<]+)', html_content)
                                        if match:
                                            import html as html_parser
                                            translated_text = html_parser.unescape(match.group(1).strip())
                                except Exception:
                                    pass
                                    
                                if not translated_text:
                                    # Fallback legacy
                                    try:
                                        url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=km&dt=t&q=" + urllib.parse.quote(clean_text)
                                        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                                        with urllib.request.urlopen(req, timeout=8) as response:
                                            res_data = response.read().decode('utf-8')
                                            parsed_json = json.loads(res_data)
                                            translated_text = "".join(item[0] for item in parsed_json[0] if item[0])
                                            translated_text = translated_text.strip()
                                    except Exception as err:
                                        print("Server single translate err:", err)
                                        translated_text = clean_text
                                        
                                translated_map[clean_text] = translated_text
                
                translated_blocks = []
                for b in parsed_blocks:
                    if b["valid"]:
                        translated_text = ""
                        if b["clean_text"]:
                            translated_text = translated_map.get(b["clean_text"], b["clean_text"])
                        translated_blocks.append(f"{b['idx']}\n{b['time_line']}\n{translated_text}{b['tag']}")
                    else:
                        translated_blocks.append(b["block"])
                        
                translated_srt = "\n\n".join(translated_blocks)
                
                response_data = {"success": True, "srt": translated_srt}
                body = json.dumps(response_data, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_error(500, f"Translation failed: {e}")
            return

        # Route the transcribe API
        elif path == '/api/transcribe':
            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in content_type:
                self.send_error(400, "Content-Type must be multipart/form-data")
                return
                
            boundary_match = re.search(r'boundary=([^;]+)', content_type)
            if not boundary_match:
                self.send_error(400, "Missing boundary in Content-Type")
                return
            boundary = boundary_match.group(1).strip().strip('"')
            
            # Read post body bytes safely in 4MB chunks
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                remaining = content_length
                chunk_size = 1024 * 1024 * 4
                chunks = []
                while remaining > 0:
                    read_size = min(remaining, chunk_size)
                    chunk = self.rfile.read(read_size)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                body_bytes = b''.join(chunks)
            else:
                body_bytes = b''
            
            form_data, files = parse_multipart(body_bytes, boundary)

            # Check if we have any videos at all (support video, video_0, video_1, ...)
            has_any_video = 'video' in files or any(f'video_{i}' in files for i in range(20)) or 'video_url' in form_data
            if not has_any_video:
                self.send_error(400, "Missing video file or video_url in payload")
                return
                
            lang_code = form_data.get('language', 'km-KH')
            gemini_key = form_data.get('gemini_key', '').strip()
            if not gemini_key:
                key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gemini_key.txt")
                if os.path.exists(key_file):
                    try:
                        with open(key_file, "r", encoding="utf-8") as kf:
                            gemini_key = kf.read().strip()
                    except Exception:
                        pass
                
            temp_dir = f"temp_srv_trans_{uuid.uuid4()}"
            os.makedirs(temp_dir, exist_ok=True)

            # Collect all video file paths in sorted order
            video_file_paths = []
            video_orig_names = []
            if 'video_url' in form_data:
                video_file_paths = ['__URL__']
                video_orig_names = ['video_from_url.mp4']
            else:
                # Look for multi-video indexed keys: video_0, video_1, ...
                v_keys = [k for k in files.keys() if re.match(r'^video_\d+$', k)]
                if v_keys:
                    v_keys.sort(key=lambda k: int(k.split('_')[1]))
                    for idx, vk in enumerate(v_keys):
                        orig_fn = files[vk].get('filename') or f"video_{idx+1}.mp4"
                        p = os.path.join(temp_dir, f"vid_{idx}.mp4")
                        with open(p, 'wb') as f: f.write(files[vk]['content'])
                        video_file_paths.append(p)
                        video_orig_names.append(orig_fn)
                elif 'video' in files:
                    orig_fn = files['video'].get('filename') or "video.mp4"
                    p = os.path.join(temp_dir, "vid_0.mp4")
                    with open(p, 'wb') as f: f.write(files['video']['content'])
                    video_file_paths.append(p)
                    video_orig_names.append(orig_fn)

            input_video_path = os.path.join(temp_dir, "input_video.mp4")
            
            try:
                if video_file_paths == ['__URL__']:
                    video_url = form_data['video_url'].strip()
                    print(f"[Server Transcribe] Downloading video from URL: {video_url}")
                    req = urllib.request.Request(video_url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=120) as resp, open(input_video_path, 'wb') as f:
                        f.write(resp.read())
                    video_file_paths = [input_video_path]
                    
                # Setup ffmpeg executable
                if shutil.which("ffmpeg"):
                    ffmpeg_exe = "ffmpeg"
                else:
                    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
                    
                lang_name_map = {
                    "km-KH": "Khmer (ភាសាខ្មែរ)",
                    "en-US": "English",
                    "zh-CN": "Chinese",
                    "th-TH": "Thai",
                    "vi-VN": "Vietnamese",
                    "ja-JP": "Japanese",
                    "ko-KR": "Korean"
                }
                lang_name = lang_name_map.get(lang_code, "Khmer")
                if lang_code == "auto":
                    lang_name = "Khmer"

                print(f"[Server Transcribe] Processing {len(video_file_paths)} video(s)...")

                def reindex_and_offset_srt(srt_text, time_offset_ms, start_block_idx):
                    """Re-index SRT blocks and add time_offset_ms to all timestamps."""
                    import re as _re
                    def ms_to_srt_time(ms):
                        ms = max(0, int(ms))
                        h = ms // 3600000; ms %= 3600000
                        m = ms // 60000;   ms %= 60000
                        s = ms // 1000;    ms %= 1000
                        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
                    
                    def srt_time_to_ms(t):
                        m = _re.match(r'(\d+):(\d+):(\d+)[,.](\d+)', t)
                        if not m: return 0
                        ms_part = m.group(4).ljust(3, '0')[:3]
                        return int(m.group(1))*3600000 + int(m.group(2))*60000 + int(m.group(3))*1000 + int(ms_part)
                    
                    raw_blocks = [b.strip() for b in srt_text.strip().split('\n\n') if b.strip()]
                    out_blocks = []
                    idx = start_block_idx
                    max_end_ms = 0
                    for blk in raw_blocks:
                        lines = [l.strip() for l in blk.split('\n') if l.strip()]
                        if len(lines) >= 3:
                            time_line = lines[1]
                            tm = _re.match(r'(.+?)\s*-->\s*(.+)', time_line)
                            if tm:
                                start_ms = srt_time_to_ms(tm.group(1).strip()) + time_offset_ms
                                end_ms   = srt_time_to_ms(tm.group(2).strip()) + time_offset_ms
                                if end_ms > max_end_ms:
                                    max_end_ms = end_ms
                                new_start = ms_to_srt_time(start_ms)
                                new_end   = ms_to_srt_time(end_ms)
                                text_part = '\n'.join(lines[2:])
                                out_blocks.append(f"{idx}\n{new_start} --> {new_end}\n{text_part}")
                                idx += 1
                    return out_blocks, idx, max_end_ms

                all_srt_blocks = []       # list of SRT block strings for all videos
                individual_video_srts = [] # list of {"video_name": str, "srt_filename": str, "srt": str}
                global_block_idx = 1      # global subtitle index counter
                global_time_offset_ms = 0 # cumulative timestamp offset for multi-video merge
                last_successful_gemini_model = None

                for vid_file_num, current_video_path in enumerate(video_file_paths):
                    current_orig_name = video_orig_names[vid_file_num] if vid_file_num < len(video_orig_names) else f"video_{vid_file_num + 1}.mp4"
                    print(f"[Server Transcribe] === Video {vid_file_num + 1}/{len(video_file_paths)}: {current_orig_name} ({current_video_path}) ===")
                    
                    duration_sec = get_video_duration(current_video_path, ffmpeg_exe) or 0.0

                    # ── Gemini Transcribe Mode ──────────────────────────────────────
                    if gemini_key:
                        print(f"[Server Transcribe] Video {vid_file_num+1}: Using Gemini direct audio transcription...")
                        mp3_path = os.path.join(temp_dir, f"audio_{vid_file_num}.mp3")
                        cmd_mp3 = [
                            ffmpeg_exe, "-y",
                            "-vn",
                            "-i", current_video_path,
                            "-ar", "24000",
                            "-ac", "1",
                            "-b:a", "128k",
                            mp3_path
                        ]
                        res_mp3 = subprocess.run(cmd_mp3, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        if res_mp3.returncode == 0 and os.path.exists(mp3_path):
                            try:
                                import base64
                                with open(mp3_path, "rb") as f:
                                    audio_b64 = base64.b64encode(f.read()).decode("utf-8")
                                    
                                prompt = (
                                    "You are an expert movie sound engineer, dialogue transcriber, character voice diarization specialist, and professional cinematic Khmer dubbing translator.\n"
                                    f"Listen carefully to the audio file and transcribe/translate the dialogue into standard SRT format in {lang_name} (ភាសាខ្មែរ).\n\n"
                                    "🌟 គោលការណ៍បកប្រែជាភាសាខ្មែរអោយមានន័យ (CINEMATIC & NATURAL KHMER TRANSLATION):\n"
                                    "1. បកប្រែបែបភាពយន្តរស់រវើក មិនរឹង ឬបកប្រែពាក្យមួយៗតាមរបៀបម៉ាស៊ីន (Never do literal word-for-word machine translation).\n"
                                    "2. សម្រួលពាក្យពេចន៍ និងឃ្លាសន្ទនាឱ្យក្លាយជាភាសានិយាយពិតប្រាកដរបស់ខ្មែរ ស្តាប់ទៅពិរោះ រលូន និងស៊ីសង្វាក់គ្នាតាមសាច់រឿង។\n"
                                    "3. ប្រើប្រាស់សព្វនាម និងពាក្យហៅគ្នាឱ្យសមស្របតាមទំនាក់ទំនងតួអង្គ (ឧ. គូស្នេហ៍៖ បង/អូន, មិត្តភក្តិ៖ ឯង/ខ្ញុំ/គ្នា, មនុស្សចាស់៖ ម៉ាក់/ប៉ា/កូន/ពូ/មីង/យាយ/តា, រឿងបុរាណ៖ ព្រះអង្គ/ម្ចាស់បង/ទូលបង្គំ/ម្ចាស់អូន)។\n"
                                    "4. ប្រយោគនីមួយៗត្រូវតែមានន័យពេញលេញ ច្បាស់លាស់ និងបញ្ចេញអារម្មណ៍ត្រូវតាមសាច់រឿង (កំប្លែង ស្នេហា កំសត់ ឬតានតឹង)។\n\n"
                                    "🎯 ការកំណត់សំឡេងតួអង្គឱ្យបានសុក្រឹត ១០០% (ACCURATE SPEAKER GENDER & DIARIZATION):\n"
                                    "វិភាគកម្រិតសំឡេង (Pitch & Timbre) និងបរិបទសន្ទនាឆ្លើយឆ្លងក្នុងរឿងឱ្យបានច្បាស់លាស់បំផុត៖\n"
                                    "1. FEMALE VOICE (សំឡេងស្រី/នារី/ក្មេងស្រី):\n"
                                    "   - លក្ខណៈសំឡេង៖ ស្រួច តូច ខ្ពស់ (High fundamental pitch 165Hz - 350Hz+), សំឡេងនារី ម្តាយ កូនស្រី បងស្រី ប្រពន្ធ សង្សារស្រី។\n"
                                    "   - បរិបទពាក្យសម្តី៖ ប្រើពាក្យសុភាពនារី (ចាស, ចា៎, នាងខ្ញុំ, អូន, ខ្ញុំ...)\n"
                                    "   - កាតព្វកិច្ច៖ បញ្ចប់ប្រយោគនីមួយៗដោយដាក់ ' (Female)' នៅខាងចុងបង្អស់។\n"
                                    "2. MALE VOICE (សំឡេងប្រុស/បុរស/ក្មេងប្រុស):\n"
                                    "   - លក្ខណៈសំឡេង៖ ធំ ក្រាស់ ធ្ងន់ ទាប (Low pitch 80Hz - 165Hz), សំឡេងបុរស ឪពុក កូនប្រុស បងប្រុស ប្តី សង្សារប្រុស។\n"
                                    "   - បរិបទពាក្យសម្តី៖ ប្រើពាក្យសម្តីបុរស (បាទ, បាទបង, ខ្ញុំបាទ, បង...)\n"
                                    "   - កាតព្វកិច្ច៖ បញ្ចប់ប្រយោគនីមួយៗដោយដាក់ ' (Male)' នៅខាងចុងបង្អស់។\n\n"
                                    "⚠️ ការសន្ទនាឆ្លើយឆ្លង (CONVERSATION TURNS):\n"
                                    "តាមដានការសន្ទនាឆ្លើយឆ្លងគ្នាទៅវិញទៅមក (ឧ. ម្នាក់សួរ ម្នាក់ឆ្លើយ) ដើម្បីកុំឱ្យច្រឡំភេទសំឡេងតួអង្គ។\n"
                                    "គ្រប់បន្ទាត់អក្សររត់ SRT ទាំងអស់ ដាច់ខាតត្រូវតែបញ្ចប់ដោយ ' (Female)' ឬ ' (Male)' ជានិច្ច!\n\n"
                                    "FORMAT EXAMPLE (ឧទាហរណ៍ទម្រង់):\n"
                                    "1\n"
                                    "00:00:01,200 --> 00:00:03,800\n"
                                    "បងសុខសប្បាយជាទេ? អូននឹកបងណាស់ (Female)\n\n"
                                    "2\n"
                                    "00:00:04,100 --> 00:00:06,500\n"
                                    "បាទ បងសុខសប្បាយទេ អរគុណអូនហើយ (Male)\n\n"
                                    "Rules:\n"
                                    "1. Every single subtitle block MUST end with either ' (Female)' or ' (Male)'.\n"
                                    "2. Break subtitles into natural conversational chunks (1.5 to 7 seconds each).\n"
                                    "3. Translate into natural, emotional, cinematic Khmer suitable for movie dubbing.\n"
                                    "4. Output ONLY the raw SRT subtitle content with NO markdown fences (no ```srt), NO explanations, and NO notes."
                                )
                                
                                payload = {
                                    "system_instruction": {
                                        "parts": [{
                                            "text": (
                                                "You are a professional movie subtitle translator and audio diarization AI for Khmer dubbing. "
                                                "Your core strengths are: (1) Translating foreign dialogue into natural, fluent, emotionally resonant spoken Khmer; and (2) Accurately distinguishing female from male character voices. "
                                                "You must classify each subtitle block with (Female) or (Male) strictly based on vocal pitch and dialogue context."
                                            )
                                        }]
                                    },
                                    "contents": [{
                                        "parts": [
                                            {
                                                "inlineData": {
                                                    "mimeType": "audio/mp3",
                                                    "data": audio_b64
                                                }
                                            },
                                            {
                                                "text": prompt
                                            }
                                        ]
                                    }],
                                    "generationConfig": {
                                        "temperature": 0.1
                                    }
                                }
                                
                                models_to_try = [
                                    "gemini-2.5-flash-preview-05-20",
                                    "gemini-2.5-flash",
                                    "gemini-2.0-flash",
                                    "gemini-2.5-flash-lite",
                                    "gemini-flash-latest",
                                    "gemini-1.5-flash",
                                    "gemini-2.5-pro",
                                    "gemini-1.5-pro"
                                ]
                                if last_successful_gemini_model and last_successful_gemini_model in models_to_try:
                                    models_to_try.remove(last_successful_gemini_model)
                                    models_to_try.insert(0, last_successful_gemini_model)
                                
                                gemini_res = None
                                last_err_msg = ""
                                
                                for model_name in models_to_try:
                                    try:
                                        gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={gemini_key}"
                                        gemini_payload = json.dumps(payload).encode("utf-8")
                                        gemini_req = urllib.request.Request(
                                            gemini_url,
                                            data=gemini_payload,
                                            headers={"Content-Type": "application/json"},
                                            method="POST"
                                        )
                                        with urllib.request.urlopen(gemini_req, timeout=120) as gemini_resp:
                                            gemini_res = json.loads(gemini_resp.read().decode("utf-8"))
                                            if gemini_res:
                                                last_successful_gemini_model = model_name
                                                print(f"[Server Transcribe] Video {vid_file_num+1}: Gemini model '{model_name}' succeeded!")
                                                break
                                    except urllib.error.HTTPError as http_ex:
                                        err_body = http_ex.read().decode('utf-8', errors='ignore')
                                        print(f"[Server Transcribe] Gemini {model_name} HTTP {http_ex.code}: {err_body}")
                                        try:
                                            err_json = json.loads(err_body)
                                            last_err_msg = err_json.get('error', {}).get('message', err_body)
                                        except Exception:
                                            last_err_msg = f"HTTP {http_ex.code}: {err_body[:200]}"
                                    except Exception as ex:
                                        print(f"[Server Transcribe] Gemini {model_name} error: {ex}")
                                        last_err_msg = str(ex)

                                if gemini_res and 'candidates' in gemini_res and gemini_res['candidates']:
                                    candidate = gemini_res['candidates'][0]
                                    parts = candidate.get('content', {}).get('parts', [])
                                    if parts:
                                        srt_content = parts[0].get('text', '').strip()
                                        if srt_content.startswith("```"):
                                            lines = srt_content.split("\n")
                                            if lines[0].startswith("```"):
                                                lines = lines[1:]
                                            if lines[-1].strip() == "```":
                                                lines = lines[:-1]
                                            srt_content = "\n".join(lines).strip()
                                        
                                        # Post-process: ensure EVERY dialogue block strictly ends with (Female) or (Male)
                                        raw_blocks = [b.strip() for b in srt_content.split("\n\n") if b.strip()]
                                        processed_blocks = []
                                        last_voice = "Male"
                                        for blk in raw_blocks:
                                            lines_b = [l.strip() for l in blk.split("\n") if l.strip()]
                                            if len(lines_b) >= 3:
                                                idx_val = lines_b[0]
                                                time_val = lines_b[1]
                                                text_val = " ".join(lines_b[2:]).strip()
                                                
                                                # Check existing tags accurately without word overlap
                                                is_female = bool(
                                                    re.search(r'[\(\[\{<].*?(?:female|ស្រី|woman|girl|lady|នារី|កញ្ញា|អ្នកស្រី).*?[\)\]\}>]', text_val, re.IGNORECASE) or
                                                    re.search(r'^(?:female|ស្រី|woman|នារី|កញ្ញា)\s*[:：\-–]', text_val, re.IGNORECASE)
                                                )
                                                is_male = bool(
                                                    re.search(r'[\(\[\{<].*?(?:\bmale\b|ប្រុស|\bman\b|\bboy\b|gentleman|បុរស|លោក).*?[\)\]\}>]', text_val, re.IGNORECASE) or
                                                    re.search(r'^(?:\bmale\b|ប្រុស|\bman\b|បុរស|លោក)\s*[:：\-–]', text_val, re.IGNORECASE)
                                                )
                                                
                                                clean_text = re.sub(r'[\(\[\{<].*?(?:female|\bmale\b|ស្រី|ប្រុស|woman|\bman\b|girl|boy|lady|បុរស|នារី|កញ្ញា|លោក|speaker\s*\d+).*?[\)\]\}>]', '', text_val, flags=re.IGNORECASE)
                                                clean_text = re.sub(r'^(?:female|\bmale\b|ស្រី|ប្រុស|woman|\bman\b|speaker\s*\d+)\s*[:：\-–]\s*', '', clean_text, flags=re.IGNORECASE).strip()
                                                
                                                chosen_gender = None
                                                if is_female and not is_male:
                                                    chosen_gender = "Female"
                                                elif is_male and not is_female:
                                                    chosen_gender = "Male"
                                                else:
                                                    # Khmer semantic clues
                                                    if any(k in clean_text for k in ["ចាស", "ចា៎", "នាងខ្ញុំ", "អ្នកនាង", "កញ្ញា", "ម៉ាក់", "យាយ", "អូន"]):
                                                        chosen_gender = "Female"
                                                    elif any(k in clean_text for k in ["បាទ", "បាទបង", "ខ្ញុំបាទ", "លោកពូ", "តា", "បង"]):
                                                        chosen_gender = "Male"
                                                    else:
                                                        # Acoustic verification for this slice
                                                        try:
                                                            m_time = re.match(r'(\d+):(\d+):(\d+)[,\.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,\.](\d+)', time_val)
                                                            if m_time:
                                                                s_sec = int(m_time.group(1))*3600 + int(m_time.group(2))*60 + int(m_time.group(3)) + int(m_time.group(4))/1000.0
                                                                e_sec = int(m_time.group(5))*3600 + int(m_time.group(6))*60 + int(m_time.group(7)) + int(m_time.group(8))/1000.0
                                                                dur = max(0.5, min(3.0, e_sec - s_sec))
                                                                chk_wav = os.path.join(temp_dir, f"pitch_chk_{idx_val}.wav")
                                                                cmd_chk = [
                                                                    ffmpeg_exe, "-y", "-ss", str(s_sec), "-t", str(dur),
                                                                    "-i", mp3_path, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", chk_wav
                                                                ]
                                                                subprocess.run(cmd_chk, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                                                                if os.path.exists(chk_wav):
                                                                    det_g = detect_voice_gender(chk_wav)
                                                                    try: os.remove(chk_wav)
                                                                    except: pass
                                                                    if det_g in ("Female", "Male"):
                                                                        chosen_gender = det_g
                                                        except Exception:
                                                            pass
                                                
                                                if not chosen_gender:
                                                    chosen_gender = last_voice
                                                
                                                last_voice = chosen_gender
                                                processed_blocks.append(f"{idx_val}\n{time_val}\n{clean_text} ({chosen_gender})")
                                            else:
                                                processed_blocks.append(blk)
                                        
                                        if processed_blocks:
                                            srt_content = "\n\n".join(processed_blocks).strip()
                                        
                                        if srt_content:
                                            # Record individual video SRT with name matching the original video
                                            base_name = os.path.splitext(os.path.basename(current_orig_name))[0]
                                            individual_video_srts.append({
                                                "video_name": current_orig_name,
                                                "srt_filename": f"{base_name}.srt",
                                                "srt": srt_content
                                            })

                                            if duration_sec <= 0 and os.path.exists(mp3_path):
                                                duration_sec = get_video_duration(mp3_path, ffmpeg_exe) or 0.0

                                            new_blocks, global_block_idx, max_end_ms = reindex_and_offset_srt(
                                                srt_content, global_time_offset_ms, global_block_idx
                                            )
                                            if new_blocks:
                                                all_srt_blocks.extend(new_blocks)
                                                if duration_sec > 0:
                                                    global_time_offset_ms += int(duration_sec * 1000)
                                                elif max_end_ms > global_time_offset_ms:
                                                    global_time_offset_ms = max_end_ms + 1000
                                                print(f"[Server Transcribe] Video {vid_file_num+1} ({current_orig_name}): Added {len(new_blocks)} blocks. Total: {len(all_srt_blocks)}. Cumulative offset: {global_time_offset_ms}ms")
                                                if vid_file_num + 1 < len(video_file_paths):
                                                    time.sleep(1.0)
                                                continue  # Move to next video in for loop!

                                print(f"[Server Transcribe] Video {vid_file_num+1}: Gemini unavailable ({last_err_msg}). Falling back to Google STT...")

                            except Exception as gem_ex:
                                print(f"[Server Transcribe] Video {vid_file_num+1} Gemini exception: {gem_ex}. Falling back to Google STT...")
                    
                    # ── Legacy Google STT Mode (Fallback) ───────────────────────────
                    wav_path = os.path.join(temp_dir, f"full_audio_{vid_file_num}.wav")
                    print(f"[Server Transcribe] Video {vid_file_num+1}: Extracting audio for legacy STT...")
                    cmd_wav = [
                        ffmpeg_exe, "-y",
                        "-vn",
                        "-i", current_video_path,
                        "-ar", "16000",
                        "-ac", "1",
                        "-c:a", "pcm_s16le",
                        wav_path
                    ]
                    res_wav = subprocess.run(cmd_wav, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    if res_wav.returncode != 0 or not os.path.exists(wav_path):
                        print(f"[Server Transcribe] Warning: Could not extract audio from video {vid_file_num+1}, skipping.")
                        continue
                        
                    # Probing duration
                    if duration_sec <= 0:
                        duration_sec = get_video_duration(current_video_path, ffmpeg_exe) or 0.0
                    if duration_sec <= 0:
                        try:
                            import wave
                            with wave.open(wav_path, 'rb') as w:
                                duration_sec = w.getnframes() / float(w.getframerate())
                        except:
                            pass

                    if duration_sec <= 0:
                        print(f"[Server Transcribe] Warning: Could not determine duration for video {vid_file_num+1}, skipping.")
                        continue
                        
                    print(f"[Server Transcribe] Video {vid_file_num+1} duration: {duration_sec:.1f}s. Running silence detection...")
                    # Silence detect
                    cmd_silence = [
                        ffmpeg_exe, "-i", wav_path,
                        "-filter_complex", "silencedetect=noise=-35dB:d=0.3",
                        "-f", "null", "-"
                    ]
                    res_silence = subprocess.run(cmd_silence, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    output_sil = res_silence.stderr
                    
                    starts = [float(x) for x in re.findall(r"silence_start:\s*([\d\.]+)", output_sil)]
                    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d\.]+)", output_sil)]
                    
                    slice_points = [0.0]
                    for s, e in zip(starts, ends):
                        mid = (s + e) / 2.0
                        if mid - slice_points[-1] >= 1.0:
                            slice_points.append(mid)
                    if duration_sec - slice_points[-1] >= 2.0:
                        slice_points.append(duration_sec)
                    else:
                        slice_points[-1] = duration_sec
                        
                    segments = []
                    for si in range(len(slice_points) - 1):
                        segments.append((slice_points[si], slice_points[si+1]))
                        
                    refined = []
                    for start, end in segments:
                        dur = end - start
                        if dur > 15.0:
                            num_splits = int(dur // 8.0) + 1
                            split_dur = dur / num_splits
                            for ri in range(num_splits):
                                refined.append((start + ri * split_dur, start + (ri + 1) * split_dur))
                        else:
                            refined.append((start, end))
                            
                    import speech_recognition as sr
                    
                    recognizer = sr.Recognizer()
                    srt_blocks_vid = []
                    
                    def format_time_offset(sec, offset_ms=0):
                        total_ms = int(sec * 1000) + offset_ms
                        total_ms = max(0, total_ms)
                        hrs = total_ms // 3600000; total_ms %= 3600000
                        mins = total_ms // 60000;  total_ms %= 60000
                        secs = total_ms // 1000;   ms_part = total_ms % 1000
                        return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms_part:03d}"
                        
                    actual_lang = lang_code
                    if actual_lang == "auto":
                        actual_lang = "en-US"

                    print(f"[Server Transcribe] Video {vid_file_num+1}: Transcribing {len(refined)} chunks via Google STT...")
                    for idx, (start, end) in enumerate(refined):
                        chunk_wav = os.path.join(temp_dir, f"chunk_{vid_file_num}_{idx}.wav")
                        duration = end - start
                        cmd_cut = [
                            ffmpeg_exe, "-y",
                            "-i", wav_path,
                            "-ss", f"{start:.3f}",
                            "-t", f"{duration:.3f}",
                            "-c:a", "pcm_s16le",
                            "-af", "dynaudnorm",
                            chunk_wav
                        ]
                        subprocess.run(cmd_cut, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        
                        text = ""
                        gender = "Unknown"
                        if os.path.exists(chunk_wav):
                            try:
                                gender = detect_voice_gender(chunk_wav)
                                with sr.AudioFile(chunk_wav) as source:
                                    audio_data = recognizer.record(source)
                                    text = recognizer.recognize_google(audio_data, language=actual_lang)
                                    text = text.strip()
                            except sr.UnknownValueError:
                                pass
                            except Exception as e:
                                pass
                            try: os.remove(chunk_wav)
                            except: pass
                            
                        if text:
                            if gender == "Female":
                                text = f"{text} (Female)"
                            elif gender == "Male":
                                text = f"{text} (Male)"
                            srt_blocks_vid.append(
                                f"{global_block_idx}\n"
                                f"{format_time_offset(start, global_time_offset_ms)} --> {format_time_offset(end, global_time_offset_ms)}\n"
                                f"{text}"
                            )
                            global_block_idx += 1
                        time.sleep(0.3)

                    all_srt_blocks.extend(srt_blocks_vid)
                    global_time_offset_ms += int(duration_sec * 1000)
                    print(f"[Server Transcribe] Video {vid_file_num+1} (Google STT): Got {len(srt_blocks_vid)} blocks. Total: {len(all_srt_blocks)}. Cumulative offset: {global_time_offset_ms}ms")

                # ── After all videos processed: merge and send response ──────────
                srt_content = "\n\n".join(all_srt_blocks).strip()
                if not srt_content:
                    print("[Server Transcribe] Warning: No speech detected in any video audio.")
                    response_data = {
                        "success": False,
                        "error": "មិនអាចស្គាល់សំឡេងនិយាយក្នុងវីដេអូបានទេ! (សូមបញ្ចូល Gemini API Key ឥតគិតថ្លៃ ក្នុងប្រអប់ Gemini API Key ដើម្បីបំប្លែងសំឡេងបានច្បាស់ 100%)"
                    }
                else:
                    num_total = len(all_srt_blocks)
                    num_vids = len(video_file_paths)
                    primary_name = os.path.splitext(os.path.basename(video_orig_names[0]))[0] if video_orig_names else "subtitles_khmer"
                    print(f"[Server Transcribe] All {num_vids} video(s) transcribed successfully! Total SRT blocks: {num_total}.")
                    response_data = {
                        "success": True,
                        "srt": srt_content,
                        "primary_name": primary_name,
                        "video_srts": individual_video_srts
                    }

                body = json.dumps(response_data, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
                
            except Exception as e:
                print("[Server Transcribe] Fatal error:", e)
                traceback.print_exc()
                response_data = {"success": False, "error": str(e)}
                body = json.dumps(response_data, ensure_ascii=False).encode('utf-8')
                self.send_response(500)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
            return

        else:
            self.send_error(404, "Not Found")

def get_local_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = '127.0.0.1'
    return ip

def free_port(port):
    """If port is occupied by another python process on Windows, terminate it cleanly so server can start."""
    if sys.platform != 'win32':
        return
    try:
        output = subprocess.check_output(f'netstat -ano | findstr :{port}', shell=True, text=True, errors='ignore')
        current_pid = os.getpid()
        killed_any = False
        for line in output.strip().split('\n'):
            parts = line.strip().split()
            if len(parts) >= 5 and parts[1].endswith(f':{port}') and parts[3] == 'LISTENING':
                try:
                    pid = int(parts[4])
                except ValueError:
                    continue
                if pid != current_pid and pid > 0:
                    try:
                        proc_out = subprocess.check_output(f'tasklist /FI "PID eq {pid}" /NH', shell=True, text=True, errors='ignore')
                        if 'python' in proc_out.lower():
                            print(f"[Port Cleanup] Found existing zombie Python server process (PID {pid}) on port {port}. Terminating...")
                            subprocess.run(f'taskkill /F /PID {pid}', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            killed_any = True
                    except Exception:
                        pass
        if killed_any:
            time.sleep(1)
    except Exception:
        pass

class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, RequestHandlerClass, bind_and_activate=True):
        if getattr(socket, 'has_ipv6', False):
            try:
                self.address_family = socket.AF_INET6
                self.allow_reuse_address = True
                super().__init__(('::', server_address[1]), RequestHandlerClass, bind_and_activate=False)
                # Allow dual stack (IPv4 and IPv6) on the same socket
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                if bind_and_activate:
                    self.server_bind()
                    self.server_activate()
                    return
            except Exception:
                try:
                    self.socket.close()
                except Exception:
                    pass
        # Fallback to standard IPv4
        self.address_family = socket.AF_INET
        super().__init__(server_address, RequestHandlerClass, bind_and_activate)

if __name__ == '__main__':
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)
    
    free_port(PORT)
    install_requirements()
    
    local_ip = get_local_ip()
    print(f"\n==================================================")
    print(f" Starting AI Dubbing Pro Local Server")
    print(f" Web Interface (PC): http://localhost:{PORT}/index.html")
    print(f" Alternative (PC):   http://127.0.0.1:{PORT}/index.html")
    if local_ip != '127.0.0.1':
        print(f" Mobile / LAN URL:   http://{local_ip}:{PORT}/index.html")
        print(f" Backend URL for APK: http://{local_ip}:{PORT}")
    print(f"==================================================")
    print(f" * Dual-stack IPv4 & IPv6 socket enabled")
    print(f" * Multi-threaded request processing enabled")
    print(f" * Automatic TTS engine fallback (Edge/Google) ready")
    print(f"==================================================\n")
    
    if '--open-browser' in sys.argv:
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{PORT}/index.html")).start()

    try:
        with ThreadingTCPServer(("", PORT), DubbingHandler) as httpd:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    except Exception as e:
        print(f"Server error: {e}")
