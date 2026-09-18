import tkinter as tk
from tkinter import filedialog, ttk, messagebox
import threading
import os
import sys
import subprocess
import re
import asyncio
import uuid
import shutil

# Prevent console window flashing on Windows when compiled as a GUI app
SUBPROCESS_FLAGS = 0
if sys.platform == "win32":
    SUBPROCESS_FLAGS = 0x08000000 # subprocess.CREATE_NO_WINDOW

# Setup and auto-install requirements
def install_requirements():
    try:
        import edge_tts
        import imageio_ffmpeg
    except ImportError:
        print("Installing required Python packages: edge-tts, imageio-ffmpeg...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "edge-tts", "imageio-ffmpeg"], creationflags=SUBPROCESS_FLAGS)
            print("Packages installed successfully.")
        except Exception as e:
            messagebox.showerror("Error Installing Dependencies", 
                                 f"Failed to install edge-tts or imageio-ffmpeg:\n{str(e)}\n\nPlease run 'pip install edge-tts imageio-ffmpeg' in your terminal.")

install_requirements()

import imageio_ffmpeg
import edge_tts

# SRT Time to Milliseconds helper
def time_to_ms(h, m, s, ms):
    return ((h * 3600) + (m * 60) + s) * 1000 + ms

# Parse SRT subtitles
def parse_srt(srt_path):
    with open(srt_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    content = content.replace('\r\n', '\n')
    blocks = content.strip().split('\n\n')
    subtitles = []
    
    for block in blocks:
        lines = block.split('\n')
        if len(lines) >= 3:
            idx = lines[0].strip()
            time_line = lines[1].strip()
            text = " ".join(lines[2:]).strip()
            
            match = re.match(r'(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})', time_line)
            if match:
                start_ms = time_to_ms(int(match.group(1)), int(match.group(2)), int(match.group(3)), int(match.group(4)))
                end_ms = time_to_ms(int(match.group(5)), int(match.group(6)), int(match.group(7)), int(match.group(8)))
                subtitles.append({
                    'id': idx,
                    'start_ms': start_ms,
                    'end_ms': end_ms,
                    'text': text
                })
    return subtitles

# Core Dubbing Compiler
async def compile_dubbed_video(video_path, srt_path, voice, orig_vol, tts_vol, vocal_removed, speed_rate, output_path, log_callback, auto_voice_enabled=True, progress_callback=None, mirror_video=False):
    try:
        video_paths = [p.strip() for p in video_path.split(';') if p.strip()]
        srt_paths = [p.strip() for p in srt_path.split(';') if p.strip()]

        if len(video_paths) > 1:
            num_vids = len(video_paths)
            log_callback(f" 🎬 រកឃើញវីដេអូចំនួន {num_vids} និង SRT ចំនួន {len(srt_paths)} (Batch Mode)...")

            batch_temp_dir = f"temp_batch_tk_{uuid.uuid4()}"
            os.makedirs(batch_temp_dir, exist_ok=True)
            part_files = []
            base_out, ext_out = os.path.splitext(output_path)
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

            try:
                for idx, current_video in enumerate(video_paths):
                    part_num = idx + 1
                    current_srt = srt_paths[idx] if idx < len(srt_paths) else srt_paths[min(idx, len(srt_paths) - 1)]
                    log_callback(f"\n📂 [វីដេអូទី {part_num}/{num_vids}] កំពុងដំណើរការ...")

                    part_output = f"{base_out}_vid{part_num}{ext_out}"
                    success = await compile_dubbed_video(
                        current_video, current_srt, voice, orig_vol, tts_vol,
                        vocal_removed, speed_rate, part_output, log_callback,
                        auto_voice_enabled=auto_voice_enabled, progress_callback=progress_callback,
                        mirror_video=mirror_video
                    )
                    if not success:
                        log_callback(f"❌ បញ្ចូលសំឡេងវីដេអូទី {part_num} បរាជ័យ")
                        return False

                    log_callback(f"✅ វីដេអូទី {part_num} រួចរាល់")
                    part_files.append(part_output)

                log_callback("\n🔗 កំពុងរួមបញ្ចូលវីដេអូទាំងអស់...")
                concat_txt = os.path.join(batch_temp_dir, "concat_parts.txt")
                with open(concat_txt, 'w', encoding='utf-8') as f:
                    for pf in part_files:
                        escaped = os.path.abspath(pf).replace('\\', '/')
                        f.write(f"file '{escaped}'\n")

                log_callback("⚙️ កំពុងតភ្ជាប់ និង Re-encode វីដេអូដើម្បីការពារការស្កុប/ទាក់រូបភាព...")
                concat_cmd = [
                    ffmpeg_exe, "-y",
                    "-f", "concat", "-safe", "0",
                    "-i", concat_txt,
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
                    "-c:a", "aac",
                    "-avoid_negative_ts", "make_zero",
                    output_path
                ]
                res = subprocess.run(concat_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=SUBPROCESS_FLAGS)
                if res.returncode == 0:
                    log_callback(f"🎉 ជោគជ័យ! បង្កើតវីដេអូសរុប {num_vids} ផ្នែករួចរាល់")
                    return True
                else:
                    err = res.stderr.decode('utf-8', errors='ignore')
                    log_callback(f"❌ កំហុសរួមបញ្ចូល: {err[:200]}")
                    return False
            finally:
                import shutil as _shutil
                _shutil.rmtree(batch_temp_dir, ignore_errors=True)
                for pf in part_files:
                    try: os.remove(pf)
                    except: pass

        log_callback(" កំពុងអានឯកសារអក្សររត់ SRT...")
        all_subtitles = []
        cumulative_offset_ms = 0
        
        for idx, path in enumerate(srt_paths):
            if os.path.exists(path):
                subs = parse_srt(path)
                if not subs:
                    continue
                
                # Check if this part has relative timestamps (starts before the previous ended)
                subs_sorted = sorted(subs, key=lambda x: x['start_ms'])
                first_start = subs_sorted[0]['start_ms']
                
                local_offset = 0
                if first_start < cumulative_offset_ms:
                    local_offset = cumulative_offset_ms
                    log_callback(f" ℹ️ ឯកសារទី {idx+1} ប្រើប្រាស់ម៉ោងចាប់ផ្តើមឡើងវិញ។ បូកបន្ថែម Offset: {local_offset/1000:.2f}s")
                
                for s in subs:
                    s['start_ms'] += local_offset
                    s['end_ms'] += local_offset
                    all_subtitles.append(s)
                
                max_end = max(s['end_ms'] for s in subs)
                cumulative_offset_ms = max_end
        
        # Sort all combined subtitles chronologically by start time
        subtitles = sorted(all_subtitles, key=lambda x: x['start_ms'])
        log_callback(f" រកឃើញអត្ថបទសរុបចំនួន {len(subtitles)} ឃ្លា (ពី SRT ចំនួន {len(srt_paths)})។")
        
        # Temp dir creation
        temp_dir = "temp_dubbing"
        os.makedirs(temp_dir, exist_ok=True)
        
        # Setup rate parameter
        rate_param = f"{speed_rate:+d}%" if speed_rate != 0 else "+0%"
        
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        concat_list_path = os.path.join(temp_dir, "concat_list.txt")
        concat_files = []
        current_time_ms = 0
        
        # Process and download each subtitle block
        for i, sub in enumerate(subtitles):
            if progress_callback:
                progress_callback(int((i / len(subtitles)) * 80))
            log_callback(f" [{i+1}/{len(subtitles)}] កំពុងបញ្ចូលសំឡេង៖ {sub['text'][:25]}...")
            
            # 1. Generate Silence if there's a gap
            gap_ms = sub['start_ms'] - current_time_ms
            if gap_ms > 0:
                silence_file = os.path.join(temp_dir, f"silence_{i}.wav")
                duration_sec = gap_ms / 1000.0
                cmd = [
                    ffmpeg_exe, "-y",
                    "-f", "lavfi",
                    "-i", "anullsrc=r=24000:cl=mono",
                    "-t", f"{duration_sec:.3f}",
                    "-ar", "24000",
                    "-ac", "1",
                    "-acodec", "pcm_s16le",
                    silence_file
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=SUBPROCESS_FLAGS)
                concat_files.append(silence_file)
            
            # 2. Download edge-tts audio chunk to temp mp3
            temp_mp3 = os.path.join(temp_dir, f"temp_tts_{i}.mp3")
            
            seg_voice = voice
            seg_pitch = None
            sub_text = sub['text'].strip()
            
            # Check for <dubbing> tag
            if auto_voice_enabled:
                tag_match = re.search(r'<dubbing\s+[^>]*voice="([^"]+)"[^>]*>(.*?)</dubbing>', sub_text, re.DOTALL | re.IGNORECASE)
                if tag_match:
                    seg_voice_val = tag_match.group(1).strip()
                    sub_text = tag_match.group(2).strip()
                    
                    # Map simple names or full names
                    if seg_voice_val.lower() == 'sreymom' or 'sreymom' in seg_voice_val.lower():
                        seg_voice = 'km-KH-SreymomNeural'
                    elif seg_voice_val.lower() == 'piseth' or 'piseth' in seg_voice_val.lower():
                        seg_voice = 'km-KH-PisethNeural'
                    elif seg_voice_val.lower() == 'sokha' or 'sokha' in seg_voice_val.lower():
                        seg_voice = 'km-KH-SreymomNeural'
                    elif seg_voice_val.lower() == 'chitra' or 'chitra' in seg_voice_val.lower():
                        seg_voice = 'km-KH-PisethNeural'
                    else:
                        seg_voice = seg_voice_val
                        
                    # Extract pitch if present
                    pitch_match = re.search(r'pitch="([^"]+)"', tag_match.group(0), re.IGNORECASE)
                    if pitch_match:
                        seg_pitch = pitch_match.group(1).strip()
                        if seg_pitch.isdigit():
                            seg_pitch = f"+{seg_pitch}Hz"
                        elif seg_pitch.startswith(('-', '+')) and seg_pitch[1:].isdigit():
                            if not seg_pitch.endswith('Hz') and not seg_pitch.endswith('%'):
                                seg_pitch = f"{seg_pitch}Hz"
                        log_callback(f" ℹ️ បានរកឃើញ Speaker Tag: voice={seg_voice}, pitch={seg_pitch}")
                else:
                    # Strip tags if they are present but voice is missing
                    sub_text = re.sub(r'<dubbing[^>]*>', '', sub_text, flags=re.IGNORECASE)
                    sub_text = re.sub(r'</dubbing>', '', sub_text, flags=re.IGNORECASE)
            else:
                # Strip tags to prevent raw XML being read
                sub_text = re.sub(r'<dubbing[^>]*>', '', sub_text, flags=re.IGNORECASE)
                sub_text = re.sub(r'</dubbing>', '', sub_text, flags=re.IGNORECASE)

            try:
                if seg_pitch:
                    communicate = edge_tts.Communicate(sub_text, seg_voice, rate=rate_param, pitch=seg_pitch)
                else:
                    communicate = edge_tts.Communicate(sub_text, seg_voice, rate=rate_param)
                await communicate.save(temp_mp3)
            except Exception as e:
                # Retry without pitch if it failed due to invalid pitch format
                if seg_pitch:
                    try:
                        communicate = edge_tts.Communicate(sub_text, seg_voice, rate=rate_param)
                        await communicate.save(temp_mp3)
                    except Exception as e_retry:
                        raise e_retry
                else:
                    raise e
            
            # 3. Transcode to matching WAV (24000Hz, Mono)
            chunk_wav = os.path.join(temp_dir, f"chunk_{i}.wav")
            cmd = [
                ffmpeg_exe, "-y",
                "-i", temp_mp3,
                "-filter:a", "silenceremove=start_periods=1:start_threshold=-50dB",
                "-ar", "24000",
                "-ac", "1",
                "-acodec", "pcm_s16le",
                chunk_wav
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=SUBPROCESS_FLAGS)
            
            # 4. Measure duration from size (24000 Hz, mono, 16bit = 48000 bytes/sec)
            file_size = os.path.getsize(chunk_wav)
            data_bytes = file_size - 44
            chunk_duration_sec = data_bytes / 48000.0
            chunk_duration_ms = int(chunk_duration_sec * 1000)
            
            # Calculate the time window available for this subtitle chunk to finish on time.
            # If the previous chunk ran slightly late, current_time_ms will be larger than sub['start_ms'],
            # which correctly shrinks allowed_duration_ms to force the current chunk to speed up and catch up.
            target_start_ms = max(sub['start_ms'], current_time_ms)
            allowed_duration_ms = sub['end_ms'] - target_start_ms
            if allowed_duration_ms < 300:
                allowed_duration_ms = 300
            
            if allowed_duration_ms > 100:  # Only adjust if subtitle has valid duration > 100ms
                tempo = chunk_duration_ms / allowed_duration_ms
                # Adjust only if speed is noticeably slower (e.g. tempo > 1.05)
                if tempo > 1.05:
                    tempo = min(1.8, tempo)  # Cap speed factor at 1.8x to preserve voice readability
                    speeded_wav = os.path.join(temp_dir, f"chunk_speed_{i}.wav")
                    log_callback(f" ⚠️ ឃ្លាទី {i+1} វែងពេក ({chunk_duration_ms}ms > {allowed_duration_ms}ms)។ កំពុងបង្កើនល្បឿននិយាយ {tempo:.2f}x...")
                    speed_cmd = [
                        ffmpeg_exe, "-y",
                        "-i", chunk_wav,
                        "-filter:a", f"atempo={tempo:.3f}",
                        "-ar", "24000",
                        "-ac", "1",
                        "-acodec", "pcm_s16le",
                        speeded_wav
                    ]
                    speed_result = subprocess.run(speed_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=SUBPROCESS_FLAGS)
                    if speed_result.returncode == 0 and os.path.exists(speeded_wav):
                        try:
                            os.remove(chunk_wav)
                        except Exception:
                            pass
                        chunk_wav = speeded_wav
                        
                        # Recalculate duration from new speeded WAV file size
                        file_size = os.path.getsize(chunk_wav)
                        data_bytes = file_size - 44
                        chunk_duration_sec = data_bytes / 48000.0
                        chunk_duration_ms = int(chunk_duration_sec * 1000)
            
            concat_files.append(chunk_wav)
            
            # Update timing cursor
            actual_start = sub['start_ms'] if gap_ms > 0 else current_time_ms
            current_time_ms = actual_start + chunk_duration_ms
            
            # Remove temp MP3
            try: os.remove(temp_mp3)
            except: pass
            
        # Write concat configuration file
        with open(concat_list_path, 'w', encoding='utf-8') as f:
            for file in concat_files:
                escaped_path = os.path.abspath(file).replace('\\', '/')
                f.write(f"file '{escaped_path}'\n")
                
        # Concat all wav parts into single TTS track
        if progress_callback:
            progress_callback(85)
        log_callback(" កំពុងចងក្រង និងភ្ជាប់ខ្សែសំឡេងបកប្រែចូលគ្នា...")
        tts_full_wav = os.path.join(temp_dir, "tts_full.wav")
        cmd = [
            ffmpeg_exe, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list_path,
            "-c", "copy",
            tts_full_wav
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=SUBPROCESS_FLAGS)
        
        # Check if original video has an audio stream
        has_audio = True
        ffprobe_exe = ffmpeg_exe.replace('ffmpeg', 'ffprobe')
        if os.path.exists(ffprobe_exe):
            try:
                probe_cmd = [
                    ffprobe_exe, "-v", "error", 
                    "-select_streams", "a", 
                    "-show_entries", "stream=codec_type", 
                    "-of", "csv=p=0", 
                    video_path
                ]
                res = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10, creationflags=SUBPROCESS_FLAGS)
                if not res.stdout.strip():
                    has_audio = False
                    log_callback(" ⚠️ វីដេអូដើមគ្មានសំឡេងទេ។ កំពុងបញ្ចូលសំឡេងបកប្រែផ្ទាល់...")
            except Exception as e:
                print("Failed probing audio streams:", e)
                
        # Audio Mix and Video Render
        if progress_callback:
            progress_callback(95)
        log_callback(" កំពុងលាយ និងបញ្ចូលសំឡេងបកប្រែទៅក្នុងវីដេអូ...")
        
        # Compute volume ratios
        orig_vol_ratio = orig_vol / 100.0
        tts_vol_ratio = tts_vol / 100.0
        
        if vocal_removed:
            orig_vol_ratio *= 0.15 # Heavily dim original voice if vocal remover is checked
            
        v_filter_args = ["-vf", "hflip"] if mirror_video else []
        if has_audio:
            cmd = [
                ffmpeg_exe, "-y",
                "-i", video_path,
                "-i", tts_full_wav,
                "-filter_complex", f"[0:a]aresample=async=1:osr=44100,volume={orig_vol_ratio:.3f}[orig]; [1:a]aresample=async=1:osr=44100,volume={tts_vol_ratio:.3f}[tts]; [orig][tts]amix=inputs=2:duration=first:normalize=0[aout]",
                "-map", "0:v",
                "-map", "[aout]"
            ] + v_filter_args + [
                "-c:v", "libx264",
                "-preset", "superfast",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",   # Convert audio track to AAC
                output_path
            ]
        else:
            # Silent video: overlay TTS audio track directly
            cmd = [
                ffmpeg_exe, "-y",
                "-i", video_path,
                "-i", tts_full_wav,
                "-map", "0:v",
                "-map", "1:a"
            ] + v_filter_args + [
                "-c:v", "libx264",
                "-preset", "superfast",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                output_path
            ]
        
        process = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=SUBPROCESS_FLAGS)
        
        # Cleanup
        log_callback(" កំពុងលុបឯកសារបណ្តោះអាសន្ន...")
        for file in concat_files:
            try: os.remove(file)
            except: pass
        try:
            os.remove(concat_list_path)
            os.remove(tts_full_wav)
            os.rmdir(temp_dir)
        except:
            pass
            
        if process.returncode == 0:
            if progress_callback:
                progress_callback(100)
            log_callback(" ជោគជ័យ! បង្កើតវីដេអូរួចរាល់។")
            messagebox.showinfo("ជោគជ័យ", f"ការបញ្ចូលសំឡេងបានសម្រេច! វីដេអូថ្មីរក្សាទុកនៅ៖\n{output_path}")
            return True
        else:
            err_msg = process.stderr.decode('utf-8', errors='ignore')
            log_callback(f" កំហុស FFmpeg: {err_msg}")
            messagebox.showerror("កំហុស", f"កំហុសក្នុងការលាយបញ្ចូលសំឡេង៖\n{err_msg}")
            return False
            
    except Exception as e:
        log_callback(f" កំហុសប្រព័ន្ធ៖ {str(e)}")
        messagebox.showerror("កំហុស", f"មានបញ្ហាក្នុងដំណើរការបកប្រែ៖\n{str(e)}")
        return False

# Tkinter Desktop App Layout
class DubbingApp:
    def __init__(self, root):
        self.root = root
        self.root.title("AI DUBBING PRO - ឧបករណ៍បញ្ចូលសំឡេងខ្មែរ")
        self.root.geometry("620x580")
        self.root.minsize(580, 540)
        self.root.configure(bg="#0a0813")
        
        # Design Theme Colors
        self.bg_color = "#0a0813"
        self.card_color = "#161329"
        self.accent_cyan = "#00e5ff"
        self.accent_purple = "#7c4dff"
        self.text_white = "#ffffff"
        self.text_gray = "#8c89a2"
        
        self.setup_ui()
        
    def setup_ui(self):
        # Create a main container frame for Canvas and Scrollbar
        container = tk.Frame(self.root, bg=self.bg_color)
        container.pack(fill="both", expand=True)
        
        # Create Canvas and Scrollbar
        self.main_canvas = tk.Canvas(container, bg=self.bg_color, highlightthickness=0)
        self.main_scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.main_canvas.yview)
        
        # Frame that will hold all the actual content
        self.scrollable_frame = tk.Frame(self.main_canvas, bg=self.bg_color)
        
        # Bind scroll region updates
        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.main_canvas.configure(
                scrollregion=self.main_canvas.bbox("all")
            )
        )
        
        # Create window inside canvas
        self.canvas_window = self.main_canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        
        # Ensure scrollable frame matches canvas width
        def configure_canvas_width(event):
            canvas_width = event.width
            self.main_canvas.itemconfig(self.canvas_window, width=canvas_width)
            
        self.main_canvas.bind('<Configure>', configure_canvas_width)
        self.main_canvas.configure(yscrollcommand=self.main_scrollbar.set)
        
        self.main_canvas.pack(side="left", fill="both", expand=True)
        self.main_scrollbar.pack(side="right", fill="y")
        
        # Bind MouseWheel to scroll the canvas
        def _on_mousewheel(event):
            self.main_canvas.yview_scroll(int(-1*(event.delta/120)), "units")
            
        self.main_canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # Header Brand Title (Inside scrollable_frame)
        header_frame = tk.Frame(self.scrollable_frame, bg=self.bg_color, pady=5)
        header_frame.pack(fill="x")
        
        title_label = tk.Label(header_frame, text="AI DUBBING PRO", font=("Inter", 18, "bold"), fg=self.accent_cyan, bg=self.bg_color)
        title_label.pack()
        
        subtitle_label = tk.Label(header_frame, text="បកប្រែវីដេអូទៅជាភាសាខ្មែរ (Offline Encoder) តាមរយៈ Edge TTS", font=("Segoe UI", 9), fg=self.text_gray, bg=self.bg_color)
        subtitle_label.pack(pady=2)

        # Quick Scroll Down Button in Header
        scroll_down_btn = tk.Button(header_frame, text="⬇️ អូសចុះក្រោម (Scroll to Bottom)", font=("Segoe UI", 8, "bold"), bg="#161329", fg=self.accent_cyan, activebackground="#312e81", activeforeground="white", bd=0, padx=12, pady=4, cursor="hand2", command=lambda: self.main_canvas.yview_moveto(1.0))
        scroll_down_btn.pack(pady=4)

        # Style customization
        style = ttk.Style()
        style.theme_use('default')
        style.configure('TEntry', fieldbackground=self.card_color, foreground="white", bordercolor="#2e2a47")
        style.configure('TCombobox', fieldbackground=self.card_color, background="#2e2a47", foreground="white", arrowcolor="white")
        style.configure('TProgressbar', thickness=8, troughcolor="#201c38", background=self.accent_cyan)
        
        # Main Scrollable Form Box (Inside scrollable_frame)
        form_frame = tk.Frame(self.scrollable_frame, bg=self.card_color, bd=1, relief="solid", highlightbackground="#25213b")
        form_frame.pack(padx=20, pady=5, fill="both", expand=True)
        form_frame.configure(padx=12, pady=8)
        
        # 1. Video file selection row
        tk.Label(form_frame, text="វីដេអូដើម (Original Video):", font=("Segoe UI", 9, "bold"), fg=self.text_white, bg=self.card_color).pack(anchor="w", pady=2)
        video_select_frame = tk.Frame(form_frame, bg=self.card_color)
        video_select_frame.pack(fill="x", pady=1)
        
        self.video_entry = tk.Entry(video_select_frame, bg="#201c38", fg="white", insertbackground="white", bd=1, relief="solid", highlightthickness=0)
        self.video_entry.pack(side="left", fill="x", expand=True, ipady=3, padx=(0,8))
        
        video_btn = tk.Button(video_select_frame, text="ជ្រើសរើសវីដេអូ", font=("Segoe UI", 8, "bold"), bg=self.accent_purple, fg="white", activebackground="#673ab7", activeforeground="white", bd=0, padx=10, command=self.browse_video)
        video_btn.pack(side="right", ipady=1)

        # 2. SRT file selection row
        tk.Label(form_frame, text="ឯកសារអក្សររត់ (SRT Subtitles):", font=("Segoe UI", 9, "bold"), fg=self.text_white, bg=self.card_color).pack(anchor="w", pady=2)
        srt_select_frame = tk.Frame(form_frame, bg=self.card_color)
        srt_select_frame.pack(fill="x", pady=1)
        
        self.srt_entry = tk.Entry(srt_select_frame, bg="#201c38", fg="white", insertbackground="white", bd=1, relief="solid", highlightthickness=0)
        self.srt_entry.pack(side="left", fill="x", expand=True, ipady=3, padx=(0,8))
        
        srt_btn = tk.Button(srt_select_frame, text="ជ្រើសរើស SRT", font=("Segoe UI", 8, "bold"), bg=self.accent_purple, fg="white", activebackground="#673ab7", activeforeground="white", bd=0, padx=10, command=self.browse_srt)
        srt_btn.pack(side="right", ipady=1)

        # 3. Grid for settings
        grid_frame = tk.Frame(form_frame, bg=self.card_color)
        grid_frame.pack(fill="x", pady=4)
        grid_frame.columnconfigure(0, weight=1)
        grid_frame.columnconfigure(1, weight=1)

        # Speaker dropdown selection
        tk.Label(grid_frame, text="សំឡេងលំនាំដើម (Default Speaker):", font=("Segoe UI", 8, "bold"), fg=self.text_white, bg=self.card_color).grid(row=0, column=0, sticky="w", pady=2)
        self.voice_combo = ttk.Combobox(grid_frame, values=["ពិសិដ្ឋ (Piseth - Edge M)", "ស្រីមុំ (Sreymom - Edge F)"], state="readonly")
        self.voice_combo.current(1) # Default Sreymom
        self.voice_combo.grid(row=1, column=0, sticky="ew", padx=(0,6), pady=2)

        # Speed rate adjustment dropdown
        tk.Label(grid_frame, text="ល្បឿននិយាយ (TTS Speed Rate):", font=("Segoe UI", 8, "bold"), fg=self.text_white, bg=self.card_color).grid(row=0, column=1, sticky="w", pady=2)
        self.speed_combo = ttk.Combobox(grid_frame, values=["យឺត (-10%)", "ធម្មតា (1.0x)", "លឿនល្មម (+10%)", "លឿន (+20%)"], state="readonly")
        self.speed_combo.current(1) # Default 1.0x
        self.speed_combo.grid(row=1, column=1, sticky="ew", padx=(6,0), pady=2)

        # Sliders for Mixer volume control
        mixer_frame = tk.Frame(form_frame, bg=self.card_color)
        mixer_frame.pack(fill="x", pady=2)
        
        tk.Label(mixer_frame, text="កម្រិតសំឡេង (Volume Mixer)", font=("Segoe UI", 9, "bold"), fg=self.accent_cyan, bg=self.card_color).pack(anchor="w", pady=2)
        
        # Original Slider
        slider1_frame = tk.Frame(mixer_frame, bg=self.card_color)
        slider1_frame.pack(fill="x", pady=2)
        tk.Label(slider1_frame, text="សំឡេងដើម (Original Video Vol):", font=("Segoe UI", 8), fg=self.text_gray, bg=self.card_color).pack(side="left")
        self.orig_lbl = tk.Label(slider1_frame, text="15%", font=("Segoe UI", 8, "bold"), fg=self.accent_cyan, bg=self.card_color)
        self.orig_lbl.pack(side="right")
        self.orig_scale = tk.Scale(mixer_frame, from_=0, to=100, orient="horizontal", bg="#201c38", fg="white", troughcolor="#120e25", highlightthickness=0, bd=0, activebackground=self.accent_cyan, command=lambda v: self.orig_lbl.configure(text=f"{v}%"))
        self.orig_scale.set(15)
        self.orig_scale.pack(fill="x", pady=(0,4))
        
        # TTS Slider
        slider2_frame = tk.Frame(mixer_frame, bg=self.card_color)
        slider2_frame.pack(fill="x", pady=2)
        tk.Label(slider2_frame, text="សំឡេងបកប្រែ (TTS Audio Vol):", font=("Segoe UI", 8), fg=self.text_gray, bg=self.card_color).pack(side="left")
        self.tts_lbl = tk.Label(slider2_frame, text="100%", font=("Segoe UI", 8, "bold"), fg=self.accent_cyan, bg=self.card_color)
        self.tts_lbl.pack(side="right")
        self.tts_scale = tk.Scale(mixer_frame, from_=0, to=100, orient="horizontal", bg="#201c38", fg="white", troughcolor="#120e25", highlightthickness=0, bd=0, activebackground=self.accent_cyan, command=lambda v: self.tts_lbl.configure(text=f"{v}%"))
        self.tts_scale.set(100)
        self.tts_scale.pack(fill="x", pady=(0,4))

        # Vocal Remover Checkbox
        self.vocal_var = tk.BooleanVar(value=True)
        self.vocal_chk = tk.Checkbutton(mixer_frame, text="លុបសំឡេងនិយាយដើម (Auto Remove Vocal - Stereo Only)", variable=self.vocal_var, font=("Segoe UI", 8), bg=self.card_color, fg=self.text_white, selectcolor="#201c38", activebackground=self.card_color, activeforeground="white", highlightthickness=0, bd=0)
        self.vocal_chk.pack(anchor="w", pady=2)

        # Auto Voice Tag Checkbox
        self.autovoice_var = tk.BooleanVar(value=True)
        self.autovoice_chk = tk.Checkbutton(mixer_frame, text="ស្វែងរកកូដសំឡេងស្វ័យប្រវត្តិ (Auto-detect Voice Tags)", variable=self.autovoice_var, font=("Segoe UI", 8), bg=self.card_color, fg=self.text_white, selectcolor="#201c38", activebackground=self.card_color, activeforeground="white", highlightthickness=0, bd=0)
        self.autovoice_chk.pack(anchor="w", pady=2)

        # Mirror Video Checkbox
        self.mirror_var = tk.BooleanVar(value=False)
        self.mirror_chk = tk.Checkbutton(mixer_frame, text="🪞 ត្រឡប់វីដេអូ ឆ្វេង-ស្តាំ (Mirror / Flip Horizontal)", variable=self.mirror_var, font=("Segoe UI", 8), bg=self.card_color, fg=self.text_white, selectcolor="#201c38", activebackground=self.card_color, activeforeground="white", highlightthickness=0, bd=0)
        self.mirror_chk.pack(anchor="w", pady=2)

        # Output folder selection row
        tk.Label(form_frame, text="រក្សាទុកវីដេអូបម្លែងរួចនៅឯណា (Output File Path):", font=("Segoe UI", 8, "bold"), fg=self.text_white, bg=self.card_color).pack(anchor="w", pady=2)
        output_frame = tk.Frame(form_frame, bg=self.card_color)
        output_frame.pack(fill="x", pady=1)
        
        self.output_entry = tk.Entry(output_frame, bg="#201c38", fg="white", insertbackground="white", bd=1, relief="solid", highlightthickness=0)
        self.output_entry.pack(side="left", fill="x", expand=True, ipady=3, padx=(0,8))
        
        output_btn = tk.Button(output_frame, text="រក្សាទុកជា...", font=("Segoe UI", 8, "bold"), bg=self.accent_purple, fg="white", activebackground="#673ab7", activeforeground="white", bd=0, padx=10, command=self.browse_output)
        output_btn.pack(side="right", ipady=1)

        # Progress Bar and Percentage Label Container (Inside scrollable_frame)
        self.progress_frame = tk.Frame(self.scrollable_frame, bg=self.bg_color)
        self.progress_frame.pack(padx=20, pady=(4, 4), fill="x")
        
        self.progress_bar = ttk.Progressbar(self.progress_frame, style='TProgressbar', mode='determinate')
        self.progress_bar.pack(side="left", fill="x", expand=True, padx=(0, 10))
        
        self.progress_lbl = tk.Label(self.progress_frame, text="0%", font=("Segoe UI", 9, "bold"), fg=self.accent_cyan, bg=self.bg_color, width=5)
        self.progress_lbl.pack(side="right")

        # Generate Action Button (Inside scrollable_frame)
        self.action_btn = tk.Button(self.scrollable_frame, text="ចាប់ផ្តើមបញ្ចូលសំឡេងវីដេអូ (Generate Dubbed Video)", font=("Segoe UI", 10, "bold"), bg=self.accent_cyan, fg="#0a0813", activebackground="white", activeforeground="#0a0813", bd=0, pady=8, cursor="hand2", command=self.start_dubbing)
        self.action_btn.pack(padx=20, pady=(6,8), fill="x")

        # Status logger text display box at the bottom (Inside scrollable_frame)
        log_frame = tk.Frame(self.scrollable_frame, bg=self.bg_color)
        log_frame.pack(padx=20, pady=(0,10), fill="both", expand=True)
        
        self.log_text = tk.Text(log_frame, bg="#07050f", fg="#a0a0ff", font=("Consolas", 8), wrap="word", height=4, bd=1, relief="solid", highlightbackground="#201c38")
        self.log_text.pack(side="left", fill="both", expand=True)
        
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        # Quick Scroll Up Button inside Log Frame
        scroll_up_btn = tk.Button(log_frame, text="⬆️ អូសឡើងលើ (Scroll to Top)", font=("Segoe UI", 8), bg=self.bg_color, fg=self.text_gray, activebackground=self.bg_color, activeforeground="white", bd=0, cursor="hand2", command=lambda: self.main_canvas.yview_moveto(0.0))
        scroll_up_btn.pack(side="bottom", anchor="e", pady=(4, 0))
        
        self.log(" ឧបករណ៍រួចរាល់សម្រាប់ការបញ្ចូលសំឡេង។ សូមជ្រើសរើសឯកសារ...")

    def browse_video(self):
        filenames = filedialog.askopenfilenames(title="ជ្រើសរើសវីដេអូដើម", filetypes=[("Video files", "*.mp4 *.avi *.mkv *.mov *.wmv")])
        if filenames:
            joined = ";".join(filenames)
            self.video_entry.delete(0, tk.END)
            self.video_entry.insert(0, joined)
            self.auto_propose_output(filenames[0])
            
    def browse_srt(self):
        filenames = filedialog.askopenfilenames(title="ជ្រើសរើសឯកសារអក្សររត់ SRT", filetypes=[("Subtitle files", "*.srt")])
        if filenames:
            # Join multiple selected files with semicolon ;
            joined = ";".join(filenames)
            self.srt_entry.delete(0, tk.END)
            self.srt_entry.insert(0, joined)

    def browse_output(self):
        filename = filedialog.asksaveasfilename(title="រក្សាទុកវីដេអូជា...", defaultextension=".mp4", filetypes=[("MP4 Video", "*.mp4")])
        if filename:
            self.output_entry.delete(0, tk.END)
            self.output_entry.insert(0, filename)

    def auto_propose_output(self, video_path):
        # Propose saving named [original]_dubbed.mp4 on the Desktop
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        base_name = os.path.splitext(os.path.basename(video_path))[0]
        proposed = os.path.join(desktop, f"{base_name}_dubbed.mp4")
        self.output_entry.delete(0, tk.END)
        self.output_entry.insert(0, proposed)

    def log(self, message):
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)

    def start_dubbing(self):
        video = self.video_entry.get().strip()
        srt = self.srt_entry.get().strip()
        output = self.output_entry.get().strip()
        
        video_paths = [p.strip() for p in video.split(';') if p.strip()]
        if not video_paths:
            messagebox.showerror("កំហុស", "សូមជ្រើសរើស វីដេអូដើម ដែលមានពិតប្រាកដ!")
            return
        for path in video_paths:
            if not os.path.exists(path):
                messagebox.showerror("កំហុស", f"មិនអាចស្វែងរកឯកសារវីដេអូខាងក្រោមបានទេ៖\n{path}")
                return
        
        # Check if all selected SRT files exist
        srt_paths = [p.strip() for p in srt.split(';') if p.strip()]
        if not srt_paths:
            messagebox.showerror("កំហុស", "សូមជ្រើសរើស ឯកសារអក្សររត់ SRT!")
            return
        for path in srt_paths:
            if not os.path.exists(path):
                messagebox.showerror("កំហុស", f"មិនអាចស្វែងរកឯកសារ SRT ខាងក្រោមបានទេ៖\n{path}")
                return
        if not output:
            messagebox.showerror("កំហុស", "សូមជ្រើសរើស ទីតាំងសម្រាប់រក្សាទុកវីដេអូបម្លែងរួច!")
            return

        # Prepare parameters
        voice_selection = self.voice_combo.get()
        voice = 'km-KH-PisethNeural' if "Piseth" in voice_selection else 'km-KH-SreymomNeural'
        
        speed_selection = self.speed_combo.get()
        speed_rate = 0
        if "យឺត" in speed_selection: speed_rate = -10
        elif "លឿនល្មម" in speed_selection: speed_rate = 10
        elif "លឿន" in speed_selection: speed_rate = 20
        
        orig_vol = self.orig_scale.get()
        tts_vol = self.tts_scale.get()
        vocal_removed = self.vocal_var.get()
        auto_voice = self.autovoice_var.get()
        mirror_video = self.mirror_var.get()
        
        # Reset progress bar
        self.progress_bar.configure(value=0)
        self.progress_lbl.configure(text="0%")

        # Disable button and run in background thread to keep UI alive
        self.action_btn.configure(state="disabled", text="កំពុងដំណើរការបញ្ចូលសំឡេង (Processing...)...")
        self.log_text.delete(1.0, tk.END)
        
        def update_progress(pct):
            self.root.after(0, lambda: self.progress_bar.configure(value=pct))
            self.root.after(0, lambda: self.progress_lbl.configure(text=f"{int(pct)}%"))
        
        def run_thread():
            # Create a clean loop inside the background thread for edge-tts
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            success = loop.run_until_complete(
                compile_dubbed_video(video, srt, voice, orig_vol, tts_vol, vocal_removed, speed_rate, output, self.log, auto_voice, update_progress, mirror_video=mirror_video)
            )
            loop.close()
            
            # Re-enable button on UI thread
            self.root.after(0, lambda: self.action_btn.configure(state="normal", text="ចាប់ផ្តើមបញ្ចូលសំឡេងវីដេអូ (Generate Dubbed Video)"))
            
        thread = threading.Thread(target=run_thread)
        thread.daemon = True
        thread.start()

if __name__ == "__main__":
    root = tk.Tk()
    app = DubbingApp(root)
    root.mainloop()
