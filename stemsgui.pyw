import os
import sys
import time
import shutil
import subprocess
import multiprocessing
import json
import base64
from pathlib import Path
import threading
import math

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# Intentar importar el soporte de Drag & Drop (Arrastrar y Soltar)
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False


class PipeStream:
    """Redirige las salidas estándar hacia la tubería de la interfaz filtrando barras de progreso."""
    def __init__(self, conn):
        self.conn = conn
        self.buffer = ""

    def write(self, s):
        self.buffer += s
        # Procesamos tanto saltos de línea \n como retornos de carro \r (usados por tqdm)
        while "\n" in self.buffer or "\r" in self.buffer:
            idx_n = self.buffer.find("\n")
            idx_r = self.buffer.find("\r")
            
            if idx_n != -1 and (idx_r == -1 or idx_n < idx_r):
                line = self.buffer[:idx_n]
                self.buffer = self.buffer[idx_n+1:]
            else:
                line = self.buffer[:idx_r]
                self.buffer = self.buffer[idx_r+1:]
                
            cleaned = line.strip()
            if cleaned:
                if self.is_progress_line(cleaned):
                    try:
                        self.conn.send(('progress', cleaned))
                    except Exception:
                        pass
                else:
                    try:
                        self.conn.send(('log', cleaned))
                    except Exception:
                        pass

    def is_progress_line(self, line):
        # Detectar patrones típicos de barra de progreso tqdm de Demucs
        if "|" in line and ("%" in line or "seconds/s" in line or "s/seconds" in line or "it/s" in line or "/s" in line):
            return True
        return False

    def flush(self):
        if self.buffer:
            cleaned = self.buffer.strip()
            if cleaned:
                if self.is_progress_line(cleaned):
                    try:
                        self.conn.send(('progress', cleaned))
                    except Exception:
                        pass
                else:
                    try:
                        self.conn.send(('log', cleaned))
                    except Exception:
                        pass
            self.buffer = ""


# =====================================================================
# TRABAJADOR FASE 1: DEMUCS (Al terminar, libera 100% de RAM/VRAM)
# =====================================================================
def demucs_worker(audio_path_str, hilos, modelo, shifts, overlap, conn):
    try:
        import torch
        import torchaudio
        import soundfile as sf

        # Monkey Patch para evadir el error de torchcodec en Windows
        def patched_save(uri, src, sample_rate, channels_first=True, **kwargs):
            if isinstance(src, torch.Tensor):
                arr = src.detach().cpu().numpy()
            else:
                arr = src
            if channels_first and arr.ndim == 2:
                arr = arr.T
            sf.write(str(uri), arr, sample_rate)

        def patched_load(uri, **kwargs):
            data, sr = sf.read(str(uri))
            if data.ndim == 1:
                tensor = torch.from_numpy(data).unsqueeze(0)
            else:
                tensor = torch.from_numpy(data.T)
            return tensor, sr

        torchaudio.save = patched_save
        torchaudio.save_with_torchcodec = patched_save
        torchaudio.load = patched_load
        torchaudio.load_with_torchcodec = patched_load

        audio_path = Path(audio_path_str)
        nombre = audio_path.stem
        temp_dir = audio_path.parent / "temp_separation"
        folder_stems_cancion = temp_dir / modelo / nombre

        if folder_stems_cancion.exists():
            shutil.rmtree(folder_stems_cancion)

        sys.argv = [
            "demucs", "-n", modelo, 
            "--jobs", str(hilos), 
            "--segment", "7"
        ]
        
        # Agregar parámetro de desplazamientos si es mayor a 1
        if int(shifts) > 1:
            sys.argv.extend(["--shifts", str(shifts)])
            
        # Agregar parámetro de overlap si se especifica
        if overlap:
            sys.argv.extend(["--overlap", str(overlap)])
            
        sys.argv.extend([
            "-o", str(temp_dir), 
            str(audio_path)
        ])

        old_stdout = sys.stdout
        old_stderr = sys.stderr
        redirected = PipeStream(conn)
        sys.stdout = redirected
        sys.stderr = redirected

        try:
            from demucs.separate import main
            main()
        except SystemExit as se:
            if se.code is not None and se.code != 0:
                raise RuntimeError(f"Demucs finalizó con error {se.code}")
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        conn.send(('status', (True, "OK")))

    except Exception as e:
        conn.send(('status', (False, str(e))))
    finally:
        conn.close()


# =====================================================================
# TRABAJADOR FASE 2: EMPAQUETADO OFICIAL NATIVE INSTRUMENTS (COMPILADOR DIRECTO)
# =====================================================================
def stempeg_worker(audio_path_str, user_comment, modelo, shifts, overlap, log_cb, status_cb, cancel_check):
    try:
        import mutagen
        from mutagen.easymp4 import EasyMP4
        from datetime import datetime

        if cancel_check():
            raise RuntimeError("Operación cancelada por el usuario.")

        audio_path = Path(audio_path_str)
        nombre = audio_path.stem
        path_base = audio_path.parent
        archivo_final = path_base / f"{nombre}.stem.mp4"
        temp_dir = path_base / "temp_separation"
        folder_stems_cancion = temp_dir / modelo / nombre

        # --- EXTRACCIÓN PREVIA DE CARÁTULA ---
        image_data = None
        image_mime = None
        try:
            ext = audio_path.suffix.lower()
            if ext == ".mp3":
                from mutagen.mp3 import MP3
                try:
                    audio_orig = MP3(str(audio_path))
                    apics = audio_orig.tags.getall("APIC") if audio_orig.tags else []
                    if apics:
                        apic = next((a for a in apics if a.type == 3), apics[0])
                        image_data = apic.data
                        image_mime = apic.mime
                except Exception as e:
                    log_cb(f"⚠️ No se pudo leer la carátula del MP3 original: {e}")
            elif ext == ".flac":
                from mutagen.flac import FLAC
                try:
                    audio_orig = FLAC(str(audio_path))
                    pics = getattr(audio_orig, "pictures", [])
                    if pics:
                        pic = next((p for p in pics if p.type == 3), pics[0])
                        image_data = pic.data
                        image_mime = pic.mime
                except Exception as e:
                    log_cb(f"⚠️ No se pudo leer la carátula del FLAC original: {e}")
            elif ext == ".wav":
                from mutagen.wave import WAVE
                try:
                    audio_orig = WAVE(str(audio_path))
                    if audio_orig and audio_orig.tags:
                        apics = audio_orig.tags.getall("APIC")
                        if apics:
                            apic = next((a for a in apics if a.type == 3), apics[0])
                            image_data = apic.data
                            image_mime = apic.mime
                except Exception as e:
                    log_cb(f"⚠️ No se pudo leer la carátula del WAV original: {e}")
        except Exception as e:
            log_cb(f"⚠️ Error al preparar extracción de carátula: {e}")

        if archivo_final.exists():
            log_cb("-> El archivo final ya existe. Intentando sobrescribir...")
            try:
                archivo_final.unlink()
            except PermissionError:
                raise PermissionError("No se pudo eliminar el archivo Stem anterior. Asegúrate de que no esté cargado en reproductores.")

        log_cb("-> FASE 2: Iniciando compilación de audios...")
        
        rutas_audio = [
            audio_path,                           
            folder_stems_cancion / "drums.wav",  
            folder_stems_cancion / "bass.wav",   
            folder_stems_cancion / "other.wav",  
            folder_stems_cancion / "vocals.wav"  
        ]

        m4a_files = []
        for idx, src_file in enumerate(rutas_audio):
            if cancel_check():
                raise RuntimeError("Operación cancelada por el usuario.")
                
            if not src_file.exists():
                raise FileNotFoundError(f"Falta archivo de pista: {src_file.name}")
            
            m4a_output = folder_stems_cancion / f"{idx}.m4a"
            log_cb(f"  -> Codificando canal {idx}/4 a AAC de alta calidad...")
            
            cmd_ffmpeg = [
                "ffmpeg", "-y",
                "-i", str(src_file),
                "-c:a", "aac", "-b:a", "256k", "-vn",
                str(m4a_output)
            ]
            
            subprocess.run(
                cmd_ffmpeg,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True
            )
            m4a_files.append(m4a_output)

        if cancel_check():
            raise RuntimeError("Operación cancelada por el usuario.")

        log_cb("-> Generando metadata oficial de Native Instruments...")
        mis_colores = [
            {"name": "Drums",    "color": "#FF3333"},  
            {"name": "Bass",     "color": "#FF5E00"},  
            {"name": "Other",    "color": "#8CFF00"},  
            {"name": "Acapella", "color": "#FFCC00"}   
        ]
        
        metadata_ni = {
            "version": 1,
            "stems": mis_colores
        }
        
        metadata_json_str = json.dumps(metadata_ni)
        metadata_b64 = base64.b64encode(metadata_json_str.encode('utf-8')).decode('utf-8')

        log_cb("-> Ejecutando MP4Box para empaquetar contenedor compatible con Mixxx/Traktor...")
        
        call_args = ["MP4Box"]
        call_args.extend(["-add", f"{m4a_files[0]}#ID=Z", str(archivo_final)])
        for s in range(1, 5):
            call_args.extend(["-add", f"{m4a_files[s]}#ID=Z:disable"])
            
        call_args.extend([
            '-brand', 'M4A:0',
            '-rb', 'isom',
            '-rb', 'iso2',
            "-udta", f"0:type=stem:src=base64,{metadata_b64}",
            "-quiet"
        ])
        
        subprocess.run(
            call_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )

        if cancel_check():
            raise RuntimeError("Operación cancelada por el usuario.")

        # --- COPIADO DE METADATOS UTILIZANDO EASYMP4 DE MANERA RESILIENTE ---
        try:
            log_cb("-> Copiando metadatos de texto y carátula...")
            
            # Registrar soporte para 'comment' en MP3 (EasyID3) antes de leer
            from mutagen.easyid3 import EasyID3
            try:
                EasyID3.RegisterTextKey('comment', 'COMM')
            except Exception:
                pass

            orig_tags = {}
            try:
                orig_file = mutagen.File(str(audio_path), easy=True)
                if orig_file and getattr(orig_file, "tags", None):
                    # Agregamos 'description' que es la versión de comentario que usan los FLAC
                    for key in ['title', 'artist', 'album', 'genre', 'date', 'tracknumber', 'discnumber', 'comment', 'description']:
                        if key in orig_file: 
                            orig_tags[key] = orig_file[key]
            except Exception as read_err:
                log_cb(f"⚠️ Error al leer tags del original: {read_err}")

            # Inicializamos EasyMP4 de forma robusta
            final_file = EasyMP4(str(archivo_final))
            if final_file.tags is None:
                final_file.add_tags()

            # Copiar textos estables uno por uno, envolviendo cada uno en try-except
            # Esto previene que una etiqueta incompatible (como formato de tracknumber de FLAC) aborte todo el proceso.
            for key, value in orig_tags.items():
                if key not in ['comment', 'description']:
                    try:
                        # Convertir tracknumber y discnumber al formato de tuplas que requiere MP4
                        if key == 'tracknumber' and value:
                            parts = str(value[0]).split('/')
                            if len(parts) == 2:
                                final_file['tracknumber'] = [(int(parts[0]), int(parts[1]))]
                            else:
                                final_file['tracknumber'] = [(int(parts[0]), 0)]
                        elif key == 'discnumber' and value:
                            parts = str(value[0]).split('/')
                            if len(parts) == 2:
                                final_file['discnumber'] = [(int(parts[0]), int(parts[1]))]
                            else:
                                final_file['discnumber'] = [(int(parts[0]), 0)]
                        else:
                            final_file[key] = value
                    except Exception as tag_set_err:
                        log_cb(f"  [!] Omitiendo tag '{key}' debido a incompatibilidad de formato: {tag_set_err}")

            # Construir bloque de comentarios
            comment_blocks = []
            
            # 1. Comentario de usuario (si se proporcionó)
            if user_comment:
                comment_blocks.append(user_comment)
                
            # 2. Comentario original (si existía en 'comment' o 'description')
            prev_comments = orig_tags.get('comment', []) or orig_tags.get('description', [])
            if prev_comments:
                existing_comment = " / ".join(prev_comments).strip()
                if existing_comment:
                    comment_blocks.append(existing_comment)
            
            # 3. Comentario de conversión automatizado (al final)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            tipo_origen = audio_path.suffix.upper().replace(".", "")
            new_comment_line = f"Convertido desde {tipo_origen} el {now_str}. Modelo: {modelo} (Shifts: {shifts}, Overlap: {overlap}). Archivo original: {audio_path.name}"
            comment_blocks.append(new_comment_line)

            # Unir bloques secuencialmente usando exactamente dos saltos de línea (\n\n)
            try:
                final_file['comment'] = ["\n\n".join(comment_blocks)]
            except Exception as comment_err:
                log_cb(f"  [!] No se pudo guardar el comentario en MP4: {comment_err}")

            # --- INYECCIÓN DE LA CARÁTULA EN ESTE MISMO PASO DE EASYMP4 ---
            if image_data:
                from mutagen.mp4 import MP4Cover
                try:
                    log_cb("-> Integrando carátula al bloque de metadatos de EasyMP4...")
                    # Buscamos el objeto MP4Tags interno de EasyMP4 de manera ultra compatible
                    raw_mp4_tags = getattr(final_file.tags, "_EasyMP4Tags__mp4", None)
                    if raw_mp4_tags is None:
                        for k, v in final_file.tags.__dict__.items():
                            if k.endswith("__mp4"):
                                raw_mp4_tags = v
                                break
                    
                    if raw_mp4_tags is not None:
                        img_format = MP4Cover.FORMAT_PNG if "png" in str(image_mime).lower() else MP4Cover.FORMAT_JPEG
                        raw_mp4_tags["covr"] = [MP4Cover(image_data, imageformat=img_format)]
                        log_cb("  [OK] Carátula preparada para guardado unificado.")
                    else:
                        log_cb("⚠️ No se pudo acceder a la estructura interna de metadatos para la carátula.")
                except Exception as cover_err:
                    log_cb(f"⚠️ No se pudo preparar la carátula en el paso unificado: {cover_err}")

            # Guardado único (Evita corrupción de atoms)
            final_file.save()
            log_cb("  [OK] Metadatos y carátula guardados correctamente.")

        except Exception as e:
            log_cb(f"⚠️ Advertencia general de metadatos: {e}")

        # Limpiar la carpeta del demucs temporal
        if folder_stems_cancion.exists():
            shutil.rmtree(folder_stems_cancion)

        status_cb(True, "OK")

    except Exception as e:
        status_cb(False, str(e))


class StemApp(TkinterDnD.Tk if DND_AVAILABLE else tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Creador de Stems para Windows")
        self.geometry("780x820")
        self.configure(bg="#f3f3f3")
        
        self.style = ttk.Style()
        self.style.theme_use('vista')

        # Variables de control de estado del lote
        self.queue_items = []    
        self.failed_tracks = []   
        self.is_running = False
        self.is_paused = False
        self.is_cancelled = False
        self.current_process = None
        self.elapsed_seconds = 0

        ttk.Label(self, text="Creador de Stems (MP3, FLAC & WAV)", font=("Helvetica", 16, "bold")).pack(pady=10)

        if not DND_AVAILABLE:
            warn_frame = ttk.Frame(self)
            warn_frame.pack(fill=tk.X, padx=15, pady=5)
            ttk.Label(warn_frame, text="⚠️ Arrastrar y soltar no disponible. Instala tkinterdnd2.", foreground="red", font=("Helvetica", 9, "italic")).pack()

        # ==================== SECCIÓN: LISTA DE ARCHIVOS ====================
        files_frame = ttk.LabelFrame(self, text=" Canciones a Procesar (MP3, FLAC y WAV) ")
        files_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)

        list_container = ttk.Frame(files_frame)
        list_container.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.files_listbox = tk.Listbox(list_container, selectmode=tk.MULTIPLE, font=("Consolas", 10), bg="white")
        self.files_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(list_container, orient=tk.VERTICAL, command=self.files_listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.files_listbox.config(yscrollcommand=scrollbar.set)

        if DND_AVAILABLE:
            self.files_listbox.drop_target_register(DND_FILES)
            self.files_listbox.dnd_bind('<<Drop>>', self.on_drop)

        btn_frame = ttk.Frame(files_frame)
        btn_frame.pack(fill=tk.X, padx=5, pady=5)
        
        self.btn_add = ttk.Button(btn_frame, text="Añadir Audio...", command=self.select_files)
        self.btn_add.pack(side=tk.LEFT, padx=5)
        
        self.btn_remove = ttk.Button(btn_frame, text="Eliminar Seleccionados", command=self.remove_selected)
        self.btn_remove.pack(side=tk.LEFT, padx=5)
        
        self.btn_clear = ttk.Button(btn_frame, text="Limpiar Todo", command=self.clear_list)
        self.btn_clear.pack(side=tk.LEFT, padx=5)

        # ==================== SECCIÓN: CONFIGURACIÓN ====================
        options_frame = ttk.LabelFrame(self, text=" Configuración ")
        options_frame.pack(fill=tk.X, padx=15, pady=5)
        
        # Fila 0
        ttk.Label(options_frame, text="Hilos CPU:").grid(row=0, column=0, padx=10, pady=5, sticky=tk.W)
        self.spin_hilos = ttk.Spinbox(options_frame, from_=1, to=32, width=5)
        self.spin_hilos.set(1)
        self.spin_hilos.grid(row=0, column=1, padx=5, pady=5, sticky=tk.W)

        ttk.Label(options_frame, text="Reintentos:").grid(row=0, column=2, padx=20, pady=5, sticky=tk.W)
        self.spin_retries = ttk.Spinbox(options_frame, from_=1, to=5, width=5)
        self.spin_retries.set(3)
        self.spin_retries.grid(row=0, column=3, padx=5, pady=5, sticky=tk.W)

        # Fila 1 (NUEVAS OPCIONES DE MODELO Y SHIFTS)
        ttk.Label(options_frame, text="Modelo AI:").grid(row=1, column=0, padx=10, pady=5, sticky=tk.W)
        self.combo_modelo = ttk.Combobox(options_frame, values=["htdemucs", "htdemucs_ft"], width=15, state="readonly")
        self.combo_modelo.set("htdemucs_ft")
        self.combo_modelo.grid(row=1, column=1, padx=5, pady=5, sticky=tk.W)

        ttk.Label(options_frame, text="Shifts (Desv):").grid(row=1, column=2, padx=20, pady=5, sticky=tk.W)
        self.combo_shifts = ttk.Combobox(options_frame, values=["1", "2", "4", "8"], width=5, state="readonly")
        self.combo_shifts.set("1")
        self.combo_shifts.grid(row=1, column=3, padx=5, pady=5, sticky=tk.W)

        # Fila 2 (NUEVA OPCIÓN DE OVERLAP)
        ttk.Label(options_frame, text="Overlap (Solap.):").grid(row=2, column=0, padx=10, pady=5, sticky=tk.W)
        self.combo_overlap = ttk.Combobox(options_frame, values=["0.1", "0.25", "0.5", "0.75"], width=5, state="readonly")
        self.combo_overlap.set("0.25")
        self.combo_overlap.grid(row=2, column=1, padx=5, pady=5, sticky=tk.W)

        # Fila 3 (Comentario manual)
        ttk.Label(options_frame, text="Comentario Manual:").grid(row=3, column=0, padx=10, pady=5, sticky=tk.W)
        self.entry_comentario = ttk.Entry(options_frame)
        self.entry_comentario.grid(row=3, column=1, columnspan=3, padx=5, pady=5, sticky=tk.EW)
        
        options_frame.columnconfigure(1, weight=1)
        options_frame.columnconfigure(3, weight=1)

        # ==================== SECCIÓN: MÉTRICAS EN TIEMPO REAL ====================
        stats_frame = ttk.LabelFrame(self, text=" Métricas en Tiempo Real ")
        stats_frame.pack(fill=tk.X, padx=15, pady=5)
        
        stats_layout = ttk.Frame(stats_frame)
        stats_layout.pack(fill=tk.X, padx=10, pady=5)
        
        self.lbl_queue_status = ttk.Label(stats_layout, text="Cola: 0 / 0 Completados (0 pendientes)", font=("Helvetica", 9, "bold"))
        self.lbl_queue_status.pack(side=tk.LEFT, padx=10)
        
        self.lbl_elapsed_time = ttk.Label(stats_layout, text="Tiempo Transcurrido: 00:00:00", font=("Helvetica", 9))
        self.lbl_elapsed_time.pack(side=tk.LEFT, padx=20)

        self.lbl_eta = ttk.Label(stats_layout, text="ETA: --", font=("Helvetica", 9))
        self.lbl_eta.pack(side=tk.LEFT, padx=20)
        
        self.lbl_errors = ttk.Label(stats_layout, text="Errores: 0 (Click para ver)", font=("Helvetica", 9), foreground="red", cursor="hand2")
        self.lbl_errors.pack(side=tk.RIGHT, padx=10)
        self.lbl_errors.bind("<Button-1>", self.show_errors_popup)

        # ==================== SECCIÓN: PROGRESO Y LOGS ====================
        action_frame = ttk.LabelFrame(self, text=" Progreso y Logs ")
        action_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=10)
        
        action_btn_frame = ttk.Frame(action_frame)
        action_btn_frame.pack(fill=tk.X, padx=10, pady=5)
        
        self.btn_convert = ttk.Button(action_btn_frame, text="COMENZAR CONVERSIÓN", command=self.start_conversion)
        self.btn_convert.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        
        self.btn_pause = ttk.Button(action_btn_frame, text="Pausar", command=self.toggle_pause, state=tk.DISABLED)
        self.btn_pause.pack(side=tk.LEFT, padx=2)
        
        self.btn_cancel = ttk.Button(action_btn_frame, text="Cancelar", command=self.cancel_conversion, state=tk.DISABLED)
        self.btn_cancel.pack(side=tk.LEFT, padx=2)

        # Línea de progreso de un solo renglón para el tqdm
        self.lbl_current_progress = ttk.Label(action_frame, text="Progreso actual: Esperando inicio...", font=("Consolas", 10, "bold"))
        self.lbl_current_progress.pack(fill=tk.X, padx=10, pady=2)

        log_container = ttk.Frame(action_frame)
        log_container.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.log_text = tk.Text(log_container, wrap=tk.WORD, state=tk.DISABLED, bg="#1c1c1c", fg="#00ff00", font=("Consolas", 9))
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scrollbar = ttk.Scrollbar(log_container, orient=tk.VERTICAL, command=self.log_text.yview)
        log_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.config(yscrollcommand=log_scrollbar.set)
        
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    def on_drop(self, event):
        files = self.tk.splitlist(event.data)
        for f in files:
            path = Path(f.strip('{}'))
            if path.is_dir():
                for ext in ["*.mp3", "*.flac", "*.wav"]:
                    for file in path.glob(ext): 
                        self.add_file_to_list(file)
            elif path.suffix.lower() in ['.mp3', '.flac', '.wav']:
                self.add_file_to_list(path)
        self.update_stats_display()

    def select_files(self):
        files = filedialog.askopenfilenames(title="Seleccionar audios", filetypes=[("Audios", "*.mp3 *.flac *.wav")])
        for f in files: 
            self.add_file_to_list(Path(f))
        self.update_stats_display()

    def add_file_to_list(self, path):
        resolved = path.resolve()
        if not any(item["path"] == resolved for item in self.queue_items):
            self.queue_items.append({"path": resolved, "status": "pending", "duration": None})
            self.files_listbox.insert(tk.END, f"⏳ [Pendiente] {resolved.name}")

    def remove_selected(self):
        if self.is_running:
            return
        for idx in reversed(list(self.files_listbox.curselection())):
            self.files_listbox.delete(idx)
            self.queue_items.pop(idx)
        self.update_stats_display()

    def clear_list(self):
        if self.is_running:
            return
        self.files_listbox.delete(0, tk.END)
        self.queue_items.clear()
        self.failed_tracks.clear()
        self.update_stats_display()

    def update_item_status(self, idx, status, elapsed_str=""):
        if idx >= len(self.queue_items):
            return
        item = self.queue_items[idx]
        item["status"] = status
        path = item["path"]
        
        if status == "pending":
            text = f"⏳ [Pendiente] {path.name}"
        elif status == "processing":
            text = f"⚡ [Procesando...] {path.name}"
        elif status == "completed":
            text = f"✅ [Completado en {elapsed_str}] {path.name}"
        elif status == "skipped":
            text = f"⏭️ [Existente] {path.name}"
        elif status == "error":
            text = f"❌ [Error] {path.name}"
        else:
            text = f"{path.name}"
            
        self.files_listbox.delete(idx)
        self.files_listbox.insert(idx, text)

    def update_stats_display(self):
        # Excluimos de las estadísticas efectivas a los archivos omitidos porque ya existen
        total_efectivo = sum(1 for item in self.queue_items if item["status"] != "skipped")
        completados_efectivos = sum(1 for item in self.queue_items if item["status"] == "completed")
        pendientes = sum(1 for item in self.queue_items if item["status"] == "pending")
        errores = len(self.failed_tracks)
        
        self.lbl_queue_status.config(
            text=f"Cola: {completados_efectivos} / {total_efectivo} Completados ({pendientes} pendientes)"
        )
        self.lbl_errors.config(
            text=f"Errores: {errores} (Click para ver)"
        )
        self.update_eta()

    def show_errors_popup(self, event=None):
        if not self.failed_tracks:
            messagebox.showinfo("Errores", "No se han registrado errores en esta sesión.")
        else:
            msg = "Las siguientes canciones presentaron errores de procesamiento:\n\n" + \
                  "\n".join([f"- {p.name}" for p in self.failed_tracks])
            messagebox.showerror("Errores Registrados", msg)

    def update_total_timer(self):
        if self.is_running:
            # Solo acumulamos tiempo activo si no se encuentra en pausa
            if not self.is_paused:
                self.elapsed_seconds += 1
            
            hours, remainder = divmod(self.elapsed_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            self.lbl_elapsed_time.config(text=f"Tiempo Transcurrido: {hours:02d}:{minutes:02d}:{seconds:02d}")
            
            self.update_eta()
            self.after(1000, self.update_total_timer)

    def update_eta(self):
        if not self.is_running:
            self.lbl_eta.config(text="ETA: --")
            return
            
        # Contamos cuántas de las pistas activas faltan por terminar (pendientes o en procesamiento)
        pendientes = sum(1 for item in self.queue_items if item["status"] in ["pending", "processing"])
        completados_efectivos = sum(1 for item in self.queue_items if item["status"] == "completed")
        
        if completados_efectivos > 0 and pendientes > 0:
            avg_time = self.elapsed_seconds / completados_efectivos
            eta_seconds = int(avg_time * pendientes)
            eta_minutes = math.ceil(eta_seconds / 60)
            self.lbl_eta.config(text=f"ETA: ~{eta_minutes} min")
        elif pendientes == 0:
            self.lbl_eta.config(text="ETA: 0 min")
        else:
            self.lbl_eta.config(text="ETA: Calculando...")

    def toggle_pause(self):
        if not self.is_running:
            return
        if not self.is_paused:
            self.is_paused = True
            self.btn_pause.config(text="Reanudar")
            self.safe_log("\n[PAUSA] Se pausará la cola de forma limpia al terminar la pista en progreso...")
            self.lbl_current_progress.config(text="Progreso actual: [Pausa programada...] Esperando fin de pista.")
        else:
            self.is_paused = False
            self.btn_pause.config(text="Pausar")
            self.safe_log("\n[REANUDADO] Reanudando procesamiento de la cola...")
            self.lbl_current_progress.config(text="Progreso actual: Reanudando...")

    def cancel_conversion(self):
        if not self.is_running:
            return
        if messagebox.askyesno("Confirmar Cancelación", "¿Seguro que deseas abortar el procesamiento actual?"):
            self.is_cancelled = True
            self.safe_log("\n[!] Solicitud de cancelación enviada. Deteniendo tareas de inmediato...")
            self.lbl_current_progress.config(text="Progreso actual: Cancelando...")
            
            if self.current_process and self.current_process.is_alive():
                try:
                    self.current_process.terminate()
                    self.current_process.join()
                except Exception as e:
                    self.safe_log(f"Error al finalizar Demucs: {e}")

    def safe_log(self, message):
        self.after(0, self._write_to_log, message)

    def _write_to_log(self, message):
        self.log_text.config(state=tk.NORMAL)
        pos_scrollbar = self.log_text.yview()
        should_scroll = pos_scrollbar[1] > 0.9
        
        self.log_text.insert(tk.END, message + "\n")
        
        if should_scroll:
            self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def start_conversion(self):
        if not self.queue_items:
            messagebox.showwarning("Lista Vacía", "Añade al menos un archivo.")
            return
        self.set_ui_state(tk.DISABLED)
        threading.Thread(target=self.run_conversion_queue, daemon=True).start()

    def set_ui_state(self, state):
        for btn in [self.btn_convert, self.btn_add, self.btn_remove, self.btn_clear, self.spin_hilos, self.spin_retries, self.combo_modelo, self.combo_shifts, self.combo_overlap, self.entry_comentario]:
            btn.config(state=state)
            
        if state == tk.DISABLED:
            self.btn_pause.config(state=tk.NORMAL)
            self.btn_cancel.config(state=tk.NORMAL)
        else:
            self.btn_pause.config(state=tk.DISABLED, text="Pausar")
            self.btn_cancel.config(state=tk.DISABLED)

    def escuchar_pipe(self, process, conn):
        status, msg = False, "El proceso hijo falló inesperadamente."
        while process.is_alive() or conn.poll():
            if conn.poll():
                try:
                    msg_type, content = conn.recv()
                    if msg_type == 'log': 
                        self.safe_log(content)
                    elif msg_type == 'progress':
                        self.after(0, lambda c=content: self.lbl_current_progress.config(text=f"Progreso actual: {c}"))
                    elif msg_type == 'status': 
                        status, msg = content
                except EOFError: 
                    break
            else: 
                time.sleep(0.05)
        process.join()
        return status, msg

    def run_conversion_queue(self):
        self.is_running = True
        self.is_paused = False
        self.is_cancelled = False
        self.elapsed_seconds = 0
        self.failed_tracks.clear()
        
        # Recuperamos la configuración activa de la UI al presionar Iniciar
        user_comment = self.entry_comentario.get().strip()
        modelo_elegido = self.combo_modelo.get().strip()
        shifts_elegidos = int(self.combo_shifts.get())
        overlap_elegido = self.combo_overlap.get().strip()

        # --- PRE-ESCANEO DE ARCHIVOS EXISTENTES ---
        for idx, item in enumerate(self.queue_items):
            if item["status"] == "pending":
                audio_path = item["path"]
                nombre = audio_path.stem
                archivo_final = audio_path.parent / f"{nombre}.stem.mp4"
                if archivo_final.exists():
                    item["status"] = "skipped"
                    self.after(0, self.update_item_status, idx, "skipped")

        self.after(0, self.update_total_timer)
        self.after(0, self.update_stats_display)

        self.safe_log("=========================================\n   INICIANDO PROCESAMIENTO\n=========================================")

        base_paths = set()

        for idx, item in enumerate(self.queue_items):
            while self.is_paused and not self.is_cancelled:
                self.after(0, lambda: self.lbl_current_progress.config(text="Progreso actual: [PAUSADO] En pausa."))
                time.sleep(0.5)

            if self.is_cancelled:
                break

            # Omitimos del procesamiento a los ya completados y a los omitidos por existir previamente
            if item["status"] in ["completed", "skipped"]:
                continue

            audio_path = item["path"]
            nombre = audio_path.stem
            base_paths.add(audio_path.parent)
            
            self.after(0, self.update_item_status, idx, "processing")
            self.after(0, self.update_stats_display)

            self.safe_log(f"\n[{idx+1}/{len(self.queue_items)}] Procesando: {audio_path.name}")
            exito = False
            song_start_time = time.time()
            max_retries = int(self.spin_retries.get())
            hilos = self.spin_hilos.get()

            # Comprobar si ya existen los stems decodificados en el disco para saltar Demucs
            temp_dir = audio_path.parent / "temp_separation"
            folder_stems_cancion = temp_dir / modelo_elegido / nombre
            stems_requeridos = ["drums.wav", "bass.wav", "other.wav", "vocals.wav"]
            stems_existen = folder_stems_cancion.exists() and all((folder_stems_cancion / f).exists() for f in stems_requeridos)

            for intento in range(1, max_retries + 1):
                if self.is_cancelled:
                    break
                
                s1 = False
                m1 = ""
                
                if stems_existen:
                    self.safe_log("  -> [Caché] Se detectaron los stems ya decodificados. Omitiendo proceso Demucs...")
                    s1 = True
                else:
                    self.safe_log(f"  -> Intento {intento}/{max_retries}...")
                    p_conn, c_conn = multiprocessing.Pipe()
                    p1 = multiprocessing.Process(
                        target=demucs_worker, 
                        args=(str(audio_path), hilos, modelo_elegido, shifts_elegidos, overlap_elegido, p_conn)
                    )
                    self.current_process = p1
                    p1.start()
                    
                    s1, m1 = self.escuchar_pipe(p1, c_conn)
                    self.current_process = None
                    
                    if self.is_cancelled:
                        break

                    if not s1:
                        self.safe_log(f"  [!] Falló en Demucs: {m1}")
                        time.sleep(1.5)
                        continue

                result_holder = {"status": False, "msg": "No se inició el empaquetado."}
                
                def log_callback(msg):
                    self.safe_log(msg)
                    
                def status_callback(status, msg):
                    result_holder["status"] = status
                    result_holder["msg"] = msg

                p2 = threading.Thread(
                    target=stempeg_worker, 
                    args=(str(audio_path), user_comment, modelo_elegido, shifts_elegidos, overlap_elegido, log_callback, status_callback, lambda: self.is_cancelled)
                )
                p2.start()

                while p2.is_alive() and not self.is_cancelled:
                    time.sleep(0.05)
                p2.join()

                if self.is_cancelled:
                    break

                s2, m2 = result_holder["status"], result_holder["msg"]

                if s2:
                    song_elapsed = int(time.time() - song_start_time)
                    mins, secs = divmod(song_elapsed, 60)
                    elapsed_str = f"{mins:02d}:{secs:02d}"
                    
                    self.safe_log(f"  [OK] ¡Creado exitosamente en {elapsed_str}!")
                    self.after(0, self.update_item_status, idx, "completed", elapsed_str)
                    exito = True
                    break
                else:
                    self.safe_log(f"  [!] Falló en Empaquetado: {m2}")
                    # Si el empaquetado falló y estábamos usando el caché, tal vez las pistas estaban corruptas.
                    # Desactivamos stems_existen para que el siguiente intento del bucle vuelva a correr Demucs.
                    if stems_existen:
                        stems_existen = False
                        try:
                            if folder_stems_cancion.exists():
                                shutil.rmtree(folder_stems_cancion)
                        except Exception:
                            pass
                    time.sleep(1.5)

            if self.is_cancelled:
                break

            if not exito:
                self.safe_log(f"  [FALLÓ] No se pudo convertir tras {max_retries} intentos.")
                self.failed_tracks.append(audio_path)
                self.after(0, self.update_item_status, idx, "error")
                
            self.after(0, self.update_stats_display)

        self.safe_log("\n--- Limpiando temporales ---")
        for bp in base_paths:
            try:
                td = bp / "temp_separation"
                if td.exists() and not any(td.iterdir()): 
                    shutil.rmtree(td)
            except Exception: 
                pass
                
        if self.is_cancelled:
            self.safe_log("\n=========================================\n       PROCESAMIENTO CANCELADO\n=========================================")
            self.after(0, lambda: self.lbl_current_progress.config(text="Progreso actual: Cancelado."))
        else:
            self.safe_log("\n=========================================\n          ¡PROCESO FINALIZADO!\n=========================================")
            self.after(0, lambda: self.lbl_current_progress.config(text="Listo."))
            
        self.is_running = False
        self.is_paused = False
        self.is_cancelled = False
        self.after(0, lambda: self.set_ui_state(tk.NORMAL))
        self.after(0, self.update_stats_display)

    def on_closing(self):
        if self.is_running:
            if messagebox.askokcancel("Salir", "Hay tareas ejecutándose en segundo plano. ¿Seguro que quieres cerrar y cancelar todo?"):
                self.is_cancelled = True
                if self.current_process and self.current_process.is_alive():
                    try:
                        self.current_process.terminate()
                    except Exception: pass
                self.destroy()
        else:
            if messagebox.askokcancel("Salir", "¿Seguro que quieres cerrar?"): 
                self.destroy()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    app = StemApp()
    app.mainloop()

#nota para IA: siempre agrgar aqui una nueva linea con los nuevos cambios que se hicieron
#cambios desde la version anterior: añadido importacion de WAV, campo de comentario de usaurio, concatenacion del comentario original, calculo de ETA
#cambios desde la version anterior: cambiado el modelo por defecto a 'htdemucs_ft', corregido el error de rutas al cambiar de modelo y añadida la detección inteligente de stems ya decodificados para evitar repetir la fase Demucs si ya existen en caché.
#cambios desde la version anterior: añadido selector de Modelo AI (htdemucs/htdemucs_ft) y de Shifts (1/2/4/8) en la interfaz gráfica, inyección de estas opciones en el comentario automático de metadatos, y paso dinámico de parámetros a los procesos de Demucs y empaquetado.
#cambios desde la version anterior: corregido el fallo que impedía guardar los comentarios y metadatos debido a incompatibilidades de formato (como 'tracknumber' o 'discnumber') al copiar tags del archivo original, envolviendo la asignación de cada etiqueta en bloques try/except individuales e inicializando correctamente las etiquetas vacías en EasyMP4.
#cambios desde la version anterior: añadido selector de Overlap (0.1/0.25/0.5/0.75) en la interfaz gráfica, paso dinámico del parámetro '--overlap' a Demucs, e inclusión de este valor en la metadata del comentario automático.