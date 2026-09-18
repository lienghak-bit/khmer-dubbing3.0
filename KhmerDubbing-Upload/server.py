import http.server
import socketserver
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

def update_task_progress(task_id, msg):
    task = active_tasks.get(task_id)
    if not task:
        return
    task['logs'].append(msg)
    task['message'] = msg
    
    # Parse part or video progress: e.g. "📂 [វីដេអូទី 1/8]" or "📂 [ផ្នែកទី 2/4]"
    m_part = re.search(r"\[.*?\s*(\d+)/(\d+)\]", msg)
    if m_part:
        current_part = int(m_part.group(1))
        total_parts = int(m_part.group(2))
        task['current_part'] = current_part
        task['total_parts'] = total_parts
        task['progress_pct'] = int(((current_part - 1) / total_parts) * 85)
        return
        
    # Parse TTS progress: "Generating TTS for subtitle 5/10..."
    m_sub = re.search(r"subtitle\s+(\d+)/(\d+)", msg, re.IGNORECASE)
    if m_sub:
        current_sub = int(m_sub.group(1))
        total_subs = int(m_sub.group(2))
        
        if 'current_part' in task and 'total_parts' in task:
            part = task['current_part']
            tot_parts = task['total_parts']
            part_start = ((part - 1) / tot_parts) * 85
            part_end = (part / tot_parts) * 85
            part_range = part_end - part_start
            sub_progress = (current_sub / total_subs) * part_range
            task['progress_pct'] = int(part_start + sub_progress)
        else:
            task['progress_pct'] = int(5 + (current_sub / total_subs) * 80)
        return
        
    if "Merging audio" in msg or "លាយសំឡេង" in msg:
        task['progress_pct'] = 88
    elif "រួមបញ្ចូលវីដេអូ" in msg or "concat" in msg.lower():
        task['progress_pct'] = 93
    elif "Cleaning" in msg or "សម្អាត" in msg:
        task['progress_pct'] = 97
    elif "Saved permanent copy" in msg or "ជោគជ័យ" in msg or "ចម្លង" in msg:
        task['progress_pct'] = 100

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
    import wave
    import struct
    try:
        with wave.open(wav_path, 'rb') as w:
            num_frames = w.getnframes()
            sample_rate = w.getframerate()
            if num_frames == 0 or sample_rate == 0:
                return "Unknown"
            
            sec_to_read = 0.5
            frames_to_read = int(sec_to_read * sample_rate)
            if num_frames > frames_to_read:
                start_frame = (num_frames - frames_to_read) // 2
                w.setpos(start_frame)
            else:
                frames_to_read = num_frames
                
            data = w.readframes(frames_to_read)
            
        fmt = f"{len(data) // 2}h"
        samples = list(struct.unpack(fmt, data))
        
        if not samples:
            return "Unknown"
        mean_sq = sum(s*s for s in samples) / len(samples)
        rms = mean_sq ** 0.5
        if rms < 300:
            return "Unknown"
            
        window_size = min(2000, len(samples))
        window = samples[len(samples)//2 - window_size//2 : len(samples)//2 + window_size//2]
        if not window:
            window = samples[:window_size]
            
        lag_min = int(sample_rate / 300)
        lag_max = int(sample_rate / 75)
        
        best_lag = -1
        max_correlation = -float('inf')
        
        for lag in range(lag_min, lag_max + 1):
            corr = 0
            limit = len(window) - lag
            if limit <= 0:
                continue
            corr = sum(window[i] * window[i + lag] for i in range(0, limit, 2))
            if corr > max_correlation:
                max_correlation = corr
                best_lag = lag
                
        if best_lag != -1:
            freq = sample_rate / best_lag
            if 165 <= freq <= 300:
                return "Female"
            elif 75 <= freq < 165:
                return "Male"
    except Exception as e:
        print("Gender detection exception:", e)
    return "Unknown"


_BEST_H264_ENCODER = None

def get_best_h264_encoder(ffmpeg_exe):
    global _BEST_H264_ENCODER
    if _BEST_H264_ENCODER is not None:
        return _BEST_H264_ENCODER
        
    # Check GPU Hardware Encoders first (5x-10x faster than CPU)
    candidates = [
        ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23", "-pix_fmt", "yuv420p"],
        ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", "23"],
        ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-pix_fmt", "yuv420p", "-profile:v", "main", "-threads", "0"]
    ]
    for cand in candidates:
        try:
            test_cmd = [ffmpeg_exe, "-y", "-f", "lavfi", "-i", "nullsrc=s=320x240:d=0.2"] + cand + ["-f", "null", "-"]
            res = subprocess.run(test_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=4)
            if res.returncode == 0:
                _BEST_H264_ENCODER = cand
                print(f"[Server] Enabled Fast Video Encoder: {cand[1]}")
                return _BEST_H264_ENCODER
        except Exception:
            pass
            
    _BEST_H264_ENCODER = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-pix_fmt", "yuv420p", "-profile:v", "main", "-threads", "0"]
    return _BEST_H264_ENCODER


# Multi-engine TTS generator
# Tries edge-tts (Piseth/Sreymom) first; falls back to gTTS if it fails.
# -----------------------------------------------------------------------
async def generate_tts_mp3(text: str, edge_voice: str, rate_param: str, output_path: str, pitch_param: str = None) -> str:
    """
    Generate TTS audio. Returns 'edge-tts' or 'gtts' to indicate which engine succeeded.
    Raises Exception if both fail.
    """
    global active_tts_engine

    # --- Attempt 1: edge-tts (Piseth / Sreymom Neural voices) ---
    try:
        if pitch_param:
            communicate = edge_tts.Communicate(text, edge_voice, rate=rate_param, pitch=pitch_param)
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
        # Fallback to no-pitch if edge-tts failed because of an invalid pitch format
        if pitch_param:
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
            
        name_match = re.search(r'name="([^"]+)"', headers)
        if name_match:
            name = name_match.group(1)
            filename_match = re.search(r'filename="([^"]+)"', headers)
            if filename_match:
                files[name] = {
                    'filename': filename_match.group(1),
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

# Asynchronous compiler engine — handles ONE video + ONE SRT file
async def compile_single_dub_backend(video_path, srt_path, voice, orig_vol, tts_vol, vocal_removed, speed_rate, output_path, log_callback, auto_voice=False, time_offset_ms=0, blur_zones=None, mirror_video=False, task_id=None):
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
        
        # Each ms represents 48 bytes (24000Hz * 2 bytes/sample * 1 channel)
        bytes_per_ms = 48
        final_pcm = bytearray(total_ms * bytes_per_ms)
        
        temp_files_to_clean = []
        rate_param = f"{speed_rate:+d}%" if speed_rate != 0 else "+0%"

        # Concurrency semaphore: up to 12 concurrent Edge-TTS requests for fast parallel dubbing
        sem = asyncio.Semaphore(12)
        completed_count = 0
        total_subs = len(subtitles)

        async def process_subtitle(i, sub):
            nonlocal completed_count
            if task_id and active_tasks.get(task_id, {}).get('status') in ('failed', 'cancelled'):
                return i, sub, None

            temp_mp3 = os.path.join(temp_dir, f"temp_tts_{i}.mp3")
            seg_voice = voice
            seg_pitch = None
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
                    if 'sreymom' in seg_voice_val.lower() or 'sokha' in seg_voice_val.lower():
                        seg_voice = 'km-KH-SreymomNeural'
                    elif 'piseth' in seg_voice_val.lower() or 'chitra' in seg_voice_val.lower():
                        seg_voice = 'km-KH-PisethNeural'
                    else:
                        seg_voice = seg_voice_val
                        
                    pitch_match = re.search(r'pitch="([^"]+)"', tag_match.group(0), re.IGNORECASE)
                    if pitch_match:
                        seg_pitch = pitch_match.group(1).strip()
                        if seg_pitch.isdigit():
                            seg_pitch = f"+{seg_pitch}Hz"
                        elif seg_pitch.startswith(('-', '+')) and seg_pitch[1:].isdigit():
                            if not seg_pitch.endswith('Hz') and not seg_pitch.endswith('%'):
                                seg_pitch = f"{seg_pitch}Hz"
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
            loop = asyncio.get_event_loop()
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

            allowed_duration_ms = max(sub['end_ms'] - sub['start_ms'], 300)
            if allowed_duration_ms > 100 and chunk_duration_ms > allowed_duration_ms * 1.05:
                tempo = min(1.8, chunk_duration_ms / allowed_duration_ms)
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

            completed_count += 1
            log_callback(f"Generating TTS for subtitle {completed_count}/{total_subs}...")
            return i, sub, raw_pcm

        tasks = [process_subtitle(i, sub) for i, sub in enumerate(subtitles)]
        results = await asyncio.gather(*tasks)

        if task_id and active_tasks.get(task_id, {}).get('status') in ('failed', 'cancelled'):
            log_callback("Operation cancelled.")
            return False

        for i, sub, raw_pcm in sorted(results, key=lambda r: r[0]):
            if not raw_pcm:
                continue
            start_byte = sub['start_ms'] * bytes_per_ms
            write_len = min(len(raw_pcm), len(final_pcm) - start_byte)
            write_len = (write_len // 2) * 2

            if write_len > 0:
                import struct, math
                n_samples = write_len // 2
                new_s = list(struct.unpack(f'<{n_samples}h', raw_pcm[:write_len]))

                # IMPROVED FIX: 60ms cosine fade-in + fade-out on raw PCM samples.
                FADE_SAMPLES = 1440  # 60ms at 24000Hz

                fade_in_len = min(FADE_SAMPLES, n_samples)
                for fi in range(fade_in_len):
                    factor = (1.0 - math.cos(math.pi * fi / FADE_SAMPLES)) / 2.0
                    new_s[fi] = int(new_s[fi] * factor)

                fade_out_len = min(FADE_SAMPLES, n_samples)
                for fo in range(fade_out_len):
                    idx = n_samples - 1 - fo
                    if idx >= fade_in_len:
                        factor = (1.0 - math.cos(math.pi * fo / FADE_SAMPLES)) / 2.0
                        new_s[idx] = int(new_s[idx] * factor)

                existing = struct.unpack_from(f'<{n_samples}h', final_pcm, start_byte)
                mixed = struct.pack(
                    f'<{n_samples}h',
                    *[max(-32768, min(32767, e + n)) for e, n in zip(existing, new_s)]
                )
                final_pcm[start_byte : start_byte + write_len] = mixed

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

        # Check if original video has an audio stream & web-compatible video codec
        has_audio = False
        is_web_safe_h264 = False
        try:
            cmd = [ffmpeg_exe, "-i", video_path]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            if "Audio:" in res.stderr:
                has_audio = True
            # Video codec check: Chrome, Edge, Safari and Firefox in HTML5 <video> require H.264 (AVC) with yuv420p.
            # HEVC (H.265), VP9, AV1, ProRes cause a black screen with audio-only playback in Chrome!
            stderr_lower = res.stderr.lower()
            if ("video: h264" in stderr_lower or "video: avc" in stderr_lower) and "yuv420p" in stderr_lower:
                is_web_safe_h264 = True
        except Exception as e:
            print("Failed probing audio/video streams:", e)
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

        if is_web_safe_h264 and not mirror_video and not blur_zones:
            v_codec = ["-c:v", "copy"]
        else:
            best_enc = get_best_h264_encoder(ffmpeg_exe)
            if not is_web_safe_h264:
                hw_name = "Intel QuickSync GPU" if "qsv" in best_enc[1] else ("NVIDIA NVENC GPU" if "nvenc" in best_enc[1] else "CPU Fast")
                log_callback(f"⚡ បម្លែងកូដវីដេអូទៅជា H.264 Web Universal តាមរយៈ {hw_name} (ល្បឿនលឿន មិនចេញផ្ទាំងខ្មៅ)...")
            v_codec = best_enc

        if mirror_video or blur_zones:
            vid_w, vid_h = get_video_dimensions(video_path, ffmpeg_exe)
            vf_parts = []
            curr_stream = "[0:v]"

            if mirror_video:
                log_callback("🪞 កំពុងធ្វើ Mirror វីដេអូ (Flip Horizontal)...")
                next_stream = "[v_mirrored]"
                vf_parts.append(f"{curr_stream}hflip{next_stream}")
                curr_stream = next_stream

            if blur_zones:
                log_callback(f"🎨 កំពុងដាក់ Blur {len(blur_zones)} ចំណុចលើវីដេអូ ({vid_w}x{vid_h})...")
                for i, z in enumerate(blur_zones):
                    try:
                        zx = int(min(z['x'], z['x'] + z.get('w', 0)) * vid_w)
                        zy = int(min(z['y'], z['y'] + z.get('h', 0)) * vid_h)
                        zw = int(abs(z.get('w', 0)) * vid_w)
                        zh = int(abs(z.get('h', 0)) * vid_h)
                        
                        zx = max(0, min(vid_w - 4, (zx // 2) * 2))
                        zy = max(0, min(vid_h - 4, (zy // 2) * 2))
                        zw = max(4, ((min(vid_w - zx, zw)) // 2) * 2)
                        zh = max(4, ((min(vid_h - zy, zh)) // 2) * 2)
                        
                        blur_strength = int(z.get('blur', 20))
                        sigma = max(3, min(30, blur_strength // 2))

                        next_stream = f"[vblur_{i}]"
                        vf_step = f"{curr_stream}split[vmain_{i}][vcrop_{i}]; [vcrop_{i}]crop={zw}:{zh}:{zx}:{zy},gblur=sigma={sigma}:steps=2[vblur_sub_{i}]; [vmain_{i}][vblur_sub_{i}]overlay={zx}:{zy}{next_stream}"
                        vf_parts.append(vf_step)
                        curr_stream = next_stream
                    except Exception as e:
                        print(f"[Server] Error processing blur zone {i}:", e)
            if vf_parts:
                v_filter_str = "; ".join(vf_parts)
                v_map = curr_stream
                v_codec = get_best_h264_encoder(ffmpeg_exe)

        if has_audio:
            audio_filter = (
                f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={orig_vol_ratio:.3f}[orig]; "
                f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={tts_vol_ratio:.3f}[tts]; "
                f"[orig][tts]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]"
            )
        else:
            audio_filter = f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={tts_vol_ratio:.3f}[aout]"

        filter_complex = audio_filter
        if v_filter_str:
            filter_complex = f"{v_filter_str}; {audio_filter}"

        cmd = [
            ffmpeg_exe, "-y",
            "-threads", "0",
            "-i", video_path,
            "-i", tts_full_wav,
            "-filter_complex", filter_complex,
            "-map", v_map,
            "-map", "[aout]",
        ] + v_codec + [
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "44100",
            "-ac", "2",
            "-movflags", "+faststart",
            "-avoid_negative_ts", "make_zero",
            output_path
        ]
        
        process = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
        
        if process.returncode != 0:
            log_callback(f"Error: FFmpeg video mux failed: {process.stderr.decode(errors='ignore')[-500:]}")
            return False
        
        return True
        
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
    vocal_removed, speed_rate, output_path, log_callback, auto_voice=False, blur_zones=None, mirror_video=False, task_id=None
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
            log_callback(f"⚡ វីដេអូច្រើន និង SRT សរុបតែមួយ — កំពុងភ្ជាប់វីដេអូទាំងអស់បញ្ចូលគ្នាមុន (Stream Copy 0.1s)...")
            batch_temp_dir = f"temp_batch_srv_{uuid.uuid4()}"
            os.makedirs(batch_temp_dir, exist_ok=True)
            combined_video = os.path.join(batch_temp_dir, f"combined_input_{uuid.uuid4().hex[:8]}.mp4")
            concat_txt = os.path.join(batch_temp_dir, "concat_input.txt")
            with open(concat_txt, 'w', encoding='utf-8') as f:
                for vp in video_paths:
                    escaped = os.path.abspath(vp).replace('\\', '/')
                    f.write(f"file '{escaped}'\n")

            concat_cmd = [
                ffmpeg_exe, "-y",
                "-fflags", "+genpts",
                "-f", "concat", "-safe", "0",
                "-i", concat_txt,
                "-c", "copy",
                "-movflags", "+faststart",
                "-avoid_negative_ts", "make_zero",
                combined_video
            ]
            res = subprocess.run(concat_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
            if res.returncode != 0 or not os.path.exists(combined_video) or os.path.getsize(combined_video) == 0:
                log_callback("⚙️ Stream copy មិនត្រូវគ្នា — កំពុងភ្ជាប់វីដេអូដោយស្វ័យប្រវត្តិ (Safe Concat)...")
                filter_inputs = "".join(f"[{i}:v][{i}:a]" for i in range(len(video_paths)))
                fallback_cmd = [ffmpeg_exe, "-y"]
                for vp in video_paths:
                    fallback_cmd.extend(["-i", vp])
                best_enc = get_best_h264_encoder(ffmpeg_exe)
                fallback_cmd.extend([
                    "-filter_complex", f"{filter_inputs}concat=n={len(video_paths)}:v=1:a=1[v][a]",
                    "-map", "[v]", "-map", "[a]",
                ] + best_enc + [
                    "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart",
                    "-avoid_negative_ts", "make_zero",
                    combined_video
                ])
                res = subprocess.run(fallback_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1200)
                if res.returncode != 0 or not os.path.exists(combined_video):
                    log_callback("❌ បរាជ័យក្នុងការតភ្ជាប់វីដេអូបញ្ចូលគ្នា")
                    _shutil.rmtree(batch_temp_dir, ignore_errors=True)
                    return False

            log_callback("✅ តភ្ជាប់វីដេអូជោគជ័យ! ចាប់ផ្ដើមបញ្ចូលសំឡេង dubbing តែម្ដង...")
            try:
                single_srt = srt_paths[0] if srt_paths else ""
                return await compile_single_dub_backend(
                    combined_video, single_srt, voice, orig_vol, tts_vol,
                    vocal_removed, speed_rate, output_path, log_callback, auto_voice,
                    blur_zones=blur_zones, mirror_video=mirror_video, task_id=task_id
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
                    blur_zones=blur_zones, mirror_video=mirror_video, task_id=task_id
                )
                if not success:
                    log_callback(f"❌ បញ្ចូលសំឡេងវីដេអូទី {part_num} បរាជ័យ")
                    return False

                log_callback(f"✅ វីដេអូទី {part_num} រួចរាល់")
                part_files.append(part_output)

            # Concat all dubbed videos into final output
            log_callback("\n🔗 កំពុងរួមបញ្ចូលវីដេអូទាំងអស់...")
            concat_txt = os.path.join(batch_temp_dir, "concat_parts.txt")
            with open(concat_txt, 'w', encoding='utf-8') as f:
                for pf in part_files:
                    escaped = os.path.abspath(pf).replace('\\', '/')
                    f.write(f"file '{escaped}'\n")

            log_callback("⚙️ កំពុងតភ្ជាប់វីដេអូ (stream copy - លឿន)...")
            concat_cmd = [
                ffmpeg_exe, "-y",
                "-fflags", "+genpts",
                "-f", "concat", "-safe", "0",
                "-i", concat_txt,
                "-c", "copy",
                "-movflags", "+faststart",
                "-avoid_negative_ts", "make_zero",
                output_path
            ]
            res = subprocess.run(
                concat_cmd,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=900
            )
            if res.returncode != 0:
                # If stream copy concat fails (e.g. mismatched resolutions/codecs), fallback to filter concat
                log_callback("⚙️ Stream copy មិនត្រូវគ្នា — កំពុងតភ្ជាប់ និង Re-encode ដោយសុវត្ថិភាព...")
                filter_inputs = "".join(f"[{i}:v][{i}:a]" for i in range(len(part_files)))
                fallback_cmd = [ffmpeg_exe, "-y"]
                for pf in part_files:
                    fallback_cmd.extend(["-i", pf])
                best_concat_enc = get_best_h264_encoder(ffmpeg_exe)
                fallback_cmd.extend([
                    "-filter_complex", f"{filter_inputs}concat=n={len(part_files)}:v=1:a=1[v][a]",
                    "-map", "[v]", "-map", "[a]",
                ] + best_concat_enc + [
                    "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart",
                    "-avoid_negative_ts", "make_zero",
                    output_path
                ])
                res = subprocess.run(fallback_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1200)

            if res.returncode == 0:
                log_callback(f"🎉 ជោគជ័យ! បង្កើតវីដេអូសរុប {num_vids} ផ្នែករួចរាល់")
                return True
            else:
                err = res.stderr.decode('utf-8', errors='ignore')
                log_callback(f"❌ កំហុសរួមបញ្ចូល: {err[:200]}")
                return False
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
                blur_zones=blur_zones, mirror_video=mirror_video, task_id=task_id
            )
        finally:
            _shutil.rmtree(merged_temp_dir, ignore_errors=True)

    # ── SINGLE SRT MODE (default or when only 1 SRT given) ──────────────────────
    return await compile_single_dub_backend(
        video_path, srt_paths[0], voice, orig_vol, tts_vol,
        vocal_removed, speed_rate, output_path, log_callback, auto_voice, blur_zones=blur_zones, mirror_video=mirror_video, task_id=task_id
    )


def start_compilation_thread(task_id, video_path, srt_paths_joined, voice, orig_vol, tts_vol, vocal_removed, speed_rate, output_path, auto_voice, orig_filename, blur_zones=None, mirror_video=False):
    loop = asyncio.new_event_loop()
    
    def log_callback(msg):
        update_task_progress(task_id, msg)
        
    coro = compile_dubbed_video_backend(
        video_path, srt_paths_joined, voice,
        orig_vol, tts_vol, vocal_removed, speed_rate,
        output_path, log_callback, auto_voice, blur_zones, mirror_video, task_id=task_id
    )
    
    def run():
        asyncio.set_event_loop(loop)
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
                
                active_tasks[task_id]['server_output_file'] = server_output_file
                active_tasks[task_id]['output_video_path'] = output_path
                active_tasks[task_id]['progress_pct'] = 100
                active_tasks[task_id]['status'] = 'completed'
                print(f"[Server Task {task_id}] Successfully finished. Output: {server_output_file}")
            else:
                active_tasks[task_id]['status'] = 'failed'
                last_logs = "\n".join(active_tasks[task_id]['logs'][-8:]) if active_tasks[task_id]['logs'] else "No logs."
                active_tasks[task_id]['error'] = f"Compilation failed. Log tail:\n{last_logs}"
                print(f"[Server Task {task_id}] Compilation returned False.")
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"[Server Task {task_id}] Exception:\n{tb}")
            active_tasks[task_id]['status'] = 'failed'
            active_tasks[task_id]['error'] = f"Server error: {type(e).__name__}: {e}"
        finally:
            try:
                loop.close()
            except:
                pass

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
            status_data = {
                "status": task['status'],
                "progress_pct": task['progress_pct'],
                "message": task['message'],
                "current_part": task.get('current_part'),
                "total_parts": task.get('total_parts'),
                "error": task['error']
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
            pitch_param = query.get('pitch', [None])[0]

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

            temp_file = f"temp_tts_{os.getpid()}_{abs(hash(text))}.mp3"
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
            orig_vol = int(form_data.get('orig_vol', '15'))
            tts_vol = int(form_data.get('tts_vol', '100'))
            vocal_removed = form_data.get('vocal_removed', 'false').lower() == 'true'
            auto_voice = form_data.get('auto_voice', 'false').lower() == 'true'
            mirror_video = form_data.get('mirror_video', 'false').lower() == 'true'
            
            blur_zones_raw = form_data.get('blur_zones', '[]')
            try:
                blur_zones = json.loads(blur_zones_raw)
            except Exception:
                blur_zones = []
            
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
            active_tasks[task_id] = {
                'status': 'running',
                'progress_pct': 0,
                'message': 'Starting compilation...',
                'logs': [],
                'server_output_file': None,
                'output_video_path': None,
                'temp_dir': temp_dir,
                'error': None
            }

            orig_filename = None
            if 'video' in files:
                orig_filename = files['video']['filename']

            start_compilation_thread(
                task_id, input_video_path, input_srt_joined, voice,
                orig_vol, tts_vol, vocal_removed, speed_rate,
                output_video_path, auto_voice, orig_filename, blur_zones, mirror_video
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
                            "gemini-2.5-flash",
                            "gemini-2.0-flash",
                            "gemini-2.5-flash-lite",
                            "gemini-flash-latest",
                            "gemini-flash-lite-latest",
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
                        
                        if len(translated_lines) == len(original_lines):
                            line_idx = 0
                            for b in parsed_blocks:
                                if b["valid"] and b["clean_text"]:
                                    translated_map[b["clean_text"]] = translated_lines[line_idx]
                                    line_idx += 1
                            print("[Server Translate] Gemini translation successful!")
                        else:
                            raise ValueError(f"Gemini line count mismatch: {len(translated_lines)} vs {len(original_lines)}")
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
            if 'video_url' in form_data:
                video_file_paths = ['__URL__']
            else:
                # Look for multi-video indexed keys: video_0, video_1, ...
                v_keys = [k for k in files.keys() if re.match(r'^video_\d+$', k)]
                if v_keys:
                    v_keys.sort(key=lambda k: int(k.split('_')[1]))
                    for idx, vk in enumerate(v_keys):
                        p = os.path.join(temp_dir, f"vid_{idx}.mp4")
                        with open(p, 'wb') as f: f.write(files[vk]['content'])
                        video_file_paths.append(p)
                elif 'video' in files:
                    p = os.path.join(temp_dir, "vid_0.mp4")
                    with open(p, 'wb') as f: f.write(files['video']['content'])
                    video_file_paths.append(p)

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
                global_block_idx = 1      # global subtitle index counter
                global_time_offset_ms = 0 # cumulative timestamp offset for multi-video merge
                last_successful_gemini_model = None

                for vid_file_num, current_video_path in enumerate(video_file_paths):
                    print(f"[Server Transcribe] === Video {vid_file_num + 1}/{len(video_file_paths)}: {current_video_path} ===")
                    
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
                            "-b:a", "64k",
                            mp3_path
                        ]
                        res_mp3 = subprocess.run(cmd_mp3, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        if res_mp3.returncode == 0 and os.path.exists(mp3_path):
                            try:
                                import base64
                                with open(mp3_path, "rb") as f:
                                    audio_b64 = base64.b64encode(f.read()).decode("utf-8")
                                    
                                prompt = (
                                    "You are an expert movie sound engineer, dialogue transcriber, and character voice diarization specialist.\n"
                                    f"Listen carefully to the audio file and transcribe/translate the dialogue into standard SRT format in {lang_name} (ភាសាខ្មែរ).\n\n"
                                    "🎯 CRITICAL: HIGH-ACCURACY SPEAKER GENDER CLASSIFICATION (ការកំណត់ភេទសំឡេងតួអង្គច្បាស់លាស់):\n"
                                    "For EVERY dialogue block, carefully analyze the acoustic voice pitch, formant resonance, and dialogue context:\n"
                                    "1. FEMALE VOICE (សំឡេងស្រី/នារី/ក្មេងស្រី):\n"
                                    "   - Acoustic cues: Higher fundamental pitch (165Hz - 320Hz+), bright feminine timbre.\n"
                                    "   - Contextual cues: Mother, daughter, sister, wife, girlfriend, female character, politeness words (ចាស, ចា៎, នាងខ្ញុំ, អូន).\n"
                                    "   - REQUIRED: Append ' (Female)' at the very end of the dialogue line.\n"
                                    "2. MALE VOICE (សំឡេងប្រុស/បុរស/ក្មេងប្រុស):\n"
                                    "   - Acoustic cues: Lower pitch (80Hz - 165Hz), deeper masculine chest resonance.\n"
                                    "   - Contextual cues: Father, son, brother, husband, boyfriend, male character, politeness words (បាទ, ខ្ញុំបាទ, បាទបង, បង).\n"
                                    "   - REQUIRED: Append ' (Male)' at the very end of the dialogue line.\n\n"
                                    "⚠️ CONVERSATION TURNS (ការសន្ទនាឆ្លើយឆ្លង):\n"
                                    "Movie dialogues often alternate back-and-forth between a male character and a female character. "
                                    "Track EACH speaker turn accurately so that each sentence is attributed to the real speaker!\n\n"
                                    "FORMAT EXAMPLE (ឧទាហរណ៍ទម្រង់):\n"
                                    "1\n"
                                    "00:00:01,200 --> 00:00:03,800\n"
                                    "ជម្រាបសួរ តើអ្នកសុខសប្បាយជាទេ? (Female)\n\n"
                                    "2\n"
                                    "00:00:04,100 --> 00:00:06,500\n"
                                    "បាទ ខ្ញុំសុខសប្បាយធម្មតាទេ ចុះអ្នកវិញ? (Male)\n\n"
                                    "3\n"
                                    "00:00:07,000 --> 00:00:09,300\n"
                                    "ខ្ញុំក៏សុខសប្បាយដែរ អរគុណច្រើន (Female)\n\n"
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
                                                "You are a professional movie subtitle translator and audio diarization AI. "
                                                "Your core strength is accurately distinguishing female from male character voices in audio recordings. "
                                                "You must classify each subtitle block with (Female) or (Male) based on vocal pitch and dialogue context."
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
                                    "gemini-2.5-flash",
                                    "gemini-2.0-flash",
                                    "gemini-2.5-flash-lite",
                                    "gemini-flash-latest",
                                    "gemini-flash-lite-latest",
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
                                                
                                                # Check existing tags
                                                is_female = bool(
                                                    re.search(r'[\(\[\{<].*?(?:female|ស្រី|woman|girl|lady|នារី|កញ្ញា|អ្នកស្រី).*?[\)\]\}>]', text_val, re.IGNORECASE) or
                                                    re.search(r'^(?:female|ស្រី|woman|នារី|កញ្ញា)\s*[:：\-–]', text_val, re.IGNORECASE)
                                                )
                                                is_male = bool(
                                                    re.search(r'[\(\[\{<].*?(?:male|ប្រុស|man|boy|gentleman|បុរស|លោក).*?[\)\]\}>]', text_val, re.IGNORECASE) or
                                                    re.search(r'^(?:male|ប្រុស|man|បុរស|លោក)\s*[:：\-–]', text_val, re.IGNORECASE)
                                                )
                                                
                                                clean_text = re.sub(r'[\(\[\{<].*?(?:female|male|ស្រី|ប្រុស|woman|man|girl|boy|lady|បុរស|នារី|កញ្ញា|លោក|speaker\s*\d+).*?[\)\]\}>]', '', text_val, flags=re.IGNORECASE)
                                                clean_text = re.sub(r'^(?:female|male|ស្រី|ប្រុស|woman|man|speaker\s*\d+)\s*[:：\-–]\s*', '', clean_text, flags=re.IGNORECASE).strip()
                                                
                                                chosen_gender = None
                                                if is_female and not is_male:
                                                    chosen_gender = "Female"
                                                elif is_male and not is_female:
                                                    chosen_gender = "Male"
                                                else:
                                                    # Khmer semantic clues
                                                    if any(k in clean_text for k in ["ចាស", "ចា៎", "នាងខ្ញុំ", "អ្នកនាង", "កញ្ញា", "ម៉ាក់", "យាយ"]):
                                                        chosen_gender = "Female"
                                                    elif any(k in clean_text for k in ["បាទ", "បាទបង", "ខ្ញុំបាទ", "លោកពូ", "តា"]):
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
                                                print(f"[Server Transcribe] Video {vid_file_num+1} (Gemini OK): Added {len(new_blocks)} blocks. Total: {len(all_srt_blocks)}. Cumulative offset: {global_time_offset_ms}ms")
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
                    print(f"[Server Transcribe] All {num_vids} video(s) transcribed successfully! Total SRT blocks: {num_total}.")
                    response_data = {"success": True, "srt": srt_content}

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

class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

if __name__ == '__main__':
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)
    
    install_requirements()
    
    local_ip = get_local_ip()
    print(f"\n==================================================")
    print(f" Starting AI Dubbing Pro Local Server")
    print(f" Web Interface (PC): http://localhost:{PORT}/index.html")
    if local_ip != '127.0.0.1':
        print(f" Mobile / APK URL:   http://{local_ip}:{PORT}/index.html")
        print(f" Backend URL for APK: http://{local_ip}:{PORT}")
    print(f"==================================================")
    print(f" * Multi-threaded request processing enabled")
    print(f" * Automatic TTS engine fallback (Edge/Google) ready")
    print(f"==================================================\n")
    
    try:
        with ThreadingTCPServer(("", PORT), DubbingHandler) as httpd:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    except Exception as e:
        print(f"Server error: {e}")
