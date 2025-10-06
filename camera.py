import subprocess
import sys
import importlib.util
import os
import threading
import time
import numpy as np
import shutil
import queue
import json
import io
import sqlite3
from datetime import datetime
from PIL import Image, ImageTk
import cv2
from tkinter import (Tk, Label, Entry, Button, Frame, messagebox, Toplevel, 
                     filedialog, Listbox, Scrollbar, Text, END, Canvas, 
                     BOTH, simpledialog, StringVar, OptionMenu)

# --- Блок управления зависимостями ---
def run_command(command, description):
    print(description)
    try:
        subprocess.check_call(command + ['-q', '-q'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except subprocess.CalledProcessError: return False

def manage_opencv_installation():
    try:
        import cv2; _ = cv2.TrackerCSRT_create()
        return True
    except (ImportError, AttributeError):
        print("Требуется установка/обновление OpenCV."); pip_executable = sys.executable
        run_command([pip_executable, "-m", "pip", "uninstall", "-y", "opencv-python"], "Попытка удаления старой версии...")
        if run_command([pip_executable, "-m", "pip", "install", "opencv-contrib-python"], "Установка opencv-contrib-python..."):
            print("Установка завершена. Пожалуйста, перезапустите приложение."); sys.exit()
        return False

def install_package(package):
    try: importlib.import_module(package.split('[')[0]); return True
    except ImportError: return run_command([sys.executable, "-m", "pip", "install", package], f"Установка {package}...")

# --- Запуск проверок ---
if not manage_opencv_installation(): sys.exit("Не удалось настроить окружение OpenCV.")
for pkg in ["Pillow", "face_recognition"]: install_package(pkg)

import database as db

# --- Классы AsyncFrameSaver и RTSPVideoCapture ---
class AsyncFrameSaver:
    def __init__(self):
        self.save_queue = queue.Queue(); self.thread = threading.Thread(target=self._worker, daemon=True); self.is_running = False
    def _worker(self):
        while self.is_running:
            try:
                task = self.save_queue.get(timeout=1)
                if task is None: break
                filepath, frame = task; directory = os.path.dirname(filepath); os.makedirs(directory, exist_ok=True)
                cv2.imwrite(filepath, frame); self.save_queue.task_done()
            except queue.Empty: continue
            except Exception as e: print(f"[AsyncSaver] Ошибка при сохранении файла: {e}")
    def start(self): self.is_running = True; self.thread.start()
    def save(self, filepath, frame):
        if self.is_running: self.save_queue.put((filepath, frame))
    def stop(self):
        if self.is_running: self.is_running = False; self.save_queue.put(None); self.thread.join(timeout=2)

class RTSPVideoCapture:
    def __init__(self, rtsp_url):
        self.rtsp_url = rtsp_url; self.frame = None; self.ret = False; self.is_running = False; self.thread = None; self.cap = None
    def start(self):
        if self.is_running: return
        self.is_running = True; self.thread = threading.Thread(target=self.update, args=()); self.thread.daemon = True; self.thread.start()
    def update(self):
        while self.is_running:
            if self.cap is None or not self.cap.isOpened():
                print(f"[RTSP] Попытка подключения к {self.rtsp_url}..."); self.cap = cv2.VideoCapture(self.rtsp_url)
                if not self.cap.isOpened(): self.cap.release(); self.cap = None; time.sleep(5); continue
                else: print("[RTSP] Соединение установлено успешно.")
            ret, frame = self.cap.read()
            if not ret: self.cap.release(); self.cap = None; time.sleep(1); continue
            self.ret, self.frame = ret, frame
        if self.cap is not None: self.cap.release(); self.cap = None
    def read(self): return self.ret, self.frame
    def stop(self):
        self.is_running = False
        if self.thread is not None and self.thread.is_alive(): self.thread.join(timeout=2)


# --- Класс GUI приложения ---
class App:
    HISTORY_FILE = "camera_history.json"

    def __init__(self, root):
        self.root = root
        self.root.title("Face Recognition Streamer"); self.root.geometry("1200x700")
        
        db.initialize_database()

        self.is_running = False; self.video_thread = None; self.rtsp_cap = None
        self.frame_queue = queue.Queue(maxsize=2)
        self.log_queue = queue.Queue()
        self.frame_saver = AsyncFrameSaver()
        self.known_users = []; self.load_known_users()
        self.recent_detections = {}

        # --- Основная структура GUI ---
        main_frame = Frame(root)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        history_frame = Frame(main_frame, width=200, relief="solid", borderwidth=1)
        history_frame.pack(side="left", fill="y", padx=(0, 10))
        Label(history_frame, text="История подключений").pack(pady=5)
        history_scrollbar = Scrollbar(history_frame); history_scrollbar.pack(side="right", fill="y")
        self.history_listbox = Listbox(history_frame, yscrollcommand=history_scrollbar.set)
        self.history_listbox.pack(side="left", fill="both", expand=True)
        history_scrollbar.config(command=self.history_listbox.yview)
        self.history_listbox.bind("<<ListboxSelect>>", self.on_history_select); self.load_camera_history()
        
        center_frame = Frame(main_frame); center_frame.pack(side="left", fill="both", expand=True)
        setup_frame = Frame(center_frame); setup_frame.pack(pady=5, fill="x")
        
        Label(setup_frame, text="Название/Место:").pack(side="left"); self.location_entry = Entry(setup_frame, width=15); self.location_entry.insert(0, "Главный вход"); self.location_entry.pack(side="left", padx=(2, 5))
        Label(setup_frame, text="IP:").pack(side="left"); self.ip_entry = Entry(setup_frame, width=15); self.ip_entry.insert(0, "10.0.1.45"); self.ip_entry.pack(side="left", padx=(2, 5))
        Label(setup_frame, text="Порт:").pack(side="left"); self.port_entry = Entry(setup_frame, width=6); self.port_entry.insert(0, "1935"); self.port_entry.pack(side="left", padx=(2, 10))
        
        self.start_button = Button(setup_frame, text="Запустить", command=self.start_stream); self.start_button.pack(side="left")
        self.stop_button = Button(setup_frame, text="Остановить", command=self.stop_stream, state="disabled"); self.stop_button.pack(side="left", padx=5)
        
        management_frame = Frame(center_frame)
        management_frame.pack(pady=5)
        self.db_button = Button(management_frame, text="База пользователей", command=self.open_user_database_window); self.db_button.pack(side="left", padx=5)
        self.rooms_button = Button(management_frame, text="Помещения и доступ", command=self.open_rooms_management_window); self.rooms_button.pack(side="left", padx=5)
        self.cameras_button = Button(management_frame, text="Управление камерами", command=self.open_camera_management_window); self.cameras_button.pack(side="left", padx=5)
        
        self.video_label = Label(center_frame, background="black"); self.video_label.pack(expand=True, fill="both")
        log_frame = Frame(main_frame, width=350, relief="solid", borderwidth=1); log_frame.pack(side="right", fill="y", padx=(10, 0))
        Label(log_frame, text="Журнал событий").pack(pady=5)
        log_scrollbar = Scrollbar(log_frame); log_scrollbar.pack(side="right", fill="y")
        self.log_widget = Text(log_frame, yscrollcommand=log_scrollbar.set, state="disabled", width=40); self.log_widget.pack(side="left", fill="both", expand=True)
        log_scrollbar.config(command=self.log_widget.yview)
        self.log_widget.tag_configure("granted", foreground="#007ACC"); self.log_widget.tag_configure("denied", foreground="red")
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.frame_saver.start()
        self.process_log_queue()

    # --- Методы управления камерами ---
    def open_camera_management_window(self):
        self.cam_win = Toplevel(self.root)
        self.cam_win.title("Управление камерами")
        self.cam_win.geometry("700x500")
        self.cam_win.transient(self.root)
        self.cam_win.grab_set()

        Button(self.cam_win, text="Добавить новую камеру", command=self.open_add_or_edit_camera_window).pack(pady=10)

        canvas_frame = Frame(self.cam_win)
        canvas_frame.pack(fill=BOTH, expand=True)
        cam_canvas = Canvas(canvas_frame)
        scrollbar = Scrollbar(canvas_frame, orient="vertical", command=cam_canvas.yview)
        self.cam_scrollable_frame = Frame(cam_canvas)
        self.cam_scrollable_frame.bind("<Configure>", lambda e: cam_canvas.configure(scrollregion=cam_canvas.bbox("all")))
        cam_canvas.create_window((0, 0), window=self.cam_scrollable_frame, anchor="nw")
        cam_canvas.configure(yscrollcommand=scrollbar.set)
        cam_canvas.pack(side="left", fill=BOTH, expand=True)
        scrollbar.pack(side="right", fill="y")
        
        self.populate_camera_list()

    def populate_camera_list(self):
        for widget in self.cam_scrollable_frame.winfo_children():
            widget.destroy()

        cameras = db.get_all_cameras_with_rooms()
        if not cameras:
            Label(self.cam_scrollable_frame, text="Нет зарегистрированных камер.").pack(pady=20)
            return

        for cam in cameras:
            cam_frame = Frame(self.cam_scrollable_frame, borderwidth=1, relief="solid", padx=10, pady=5)
            info_text = f"IP: {cam['camera_ip']}  |  Помещение: {cam['name_rooms'] or 'Не назначено'}"
            Label(cam_frame, text=info_text, font=("Arial", 11)).pack(side="left", expand=True, fill="x")
            edit_button = Button(cam_frame, text="Редактировать", command=lambda c=cam: self.open_add_or_edit_camera_window(is_edit=True, camera_data=c))
            edit_button.pack(side="right", padx=5)
            delete_button = Button(cam_frame, text="Удалить", fg="red", command=lambda ip=cam['camera_ip']: self.delete_camera_action(ip))
            delete_button.pack(side="right")
            cam_frame.pack(fill="x", padx=10, pady=5, expand=True)

    def open_add_or_edit_camera_window(self, is_edit=False, camera_data=None):
        win = Toplevel(self.cam_win)
        win.title("Добавить камеру" if not is_edit else "Редактировать камеру")
        win.geometry("400x200")
        win.transient(self.cam_win)
        win.grab_set()

        rooms = db.get_all_rooms()
        room_choices = {room['name_rooms']: room['id_rooms'] for room in rooms}
        NO_ROOM_TEXT = "Не назначено"
        room_choices[NO_ROOM_TEXT] = None
        
        Label(win, text="IP-адрес камеры:").pack(pady=(10,0)); ip_entry = Entry(win, width=40); ip_entry.pack()
        Label(win, text="Привязать к помещению:").pack(pady=(10,0)); selected_room = StringVar(win)
        room_menu = OptionMenu(win, selected_room, *room_choices.keys()); room_menu.pack()
        
        if is_edit and camera_data:
            ip_entry.insert(0, camera_data['camera_ip'])
            selected_room.set(camera_data['name_rooms'] or NO_ROOM_TEXT)
        else:
            selected_room.set(NO_ROOM_TEXT)

        def save_camera():
            new_ip = ip_entry.get().strip()
            if not new_ip: messagebox.showerror("Ошибка", "IP-адрес не может быть пустым.", parent=win); return
            room_id = room_choices.get(selected_room.get())
            try:
                if is_edit:
                    db.update_camera(camera_data['camera_ip'], new_ip, room_id)
                    messagebox.showinfo("Успех", "Данные камеры обновлены.", parent=win)
                else:
                    db.link_camera_to_room(new_ip, room_id)
                    messagebox.showinfo("Успех", "Новая камера добавлена.", parent=win)
                win.destroy(); self.populate_camera_list()
            except sqlite3.IntegrityError: messagebox.showerror("Ошибка", f"Камера с IP '{new_ip}' уже существует.", parent=win)
            except Exception as e: messagebox.showerror("Ошибка сохранения", f"Произошла ошибка: {e}", parent=win)

        Button(win, text="Сохранить", command=save_camera).pack(pady=20)

    def delete_camera_action(self, camera_ip):
        if messagebox.askyesno("Подтверждение", f"Вы уверены, что хотите удалить камеру с IP {camera_ip}?"):
            db.delete_camera(camera_ip); messagebox.showinfo("Успех", "Камера удалена."); self.populate_camera_list()

    # --- Методы управления пользователями ---
    def load_known_users(self):
        self.known_users = db.get_known_face_encodings()
        print(f"Загружено {len(self.known_users)} пользователей из базы данных.")

    def open_user_database_window(self):
        # ... (код этого и других методов для пользователей без изменений) ...
        self.db_window = Toplevel(self.root)
        self.db_window.title("База пользователей")
        self.db_window.geometry("600x700")
        self.db_window.transient(self.root)
        self.db_window.grab_set()
        bottom_frame = Frame(self.db_window)
        bottom_frame.pack(side="bottom", fill="x", pady=10, padx=10)
        add_button = Button(bottom_frame, text="Добавить нового пользователя", command=self.open_add_user_window)
        add_button.pack()
        canvas_frame = Frame(self.db_window)
        canvas_frame.pack(fill=BOTH, expand=True)
        self.user_canvas = Canvas(canvas_frame)
        scrollbar = Scrollbar(canvas_frame, orient="vertical", command=self.user_canvas.yview)
        self.scrollable_frame = Frame(self.user_canvas)
        self.scrollable_frame.bind("<Configure>",lambda e: self.user_canvas.configure(scrollregion=self.user_canvas.bbox("all")))
        self.user_canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.user_canvas.configure(yscrollcommand=scrollbar.set)
        self.user_canvas.pack(side="left", fill=BOTH, expand=True)
        scrollbar.pack(side="right", fill="y")
        self.populate_user_list()

    def populate_user_list(self):
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()
        users = db.get_all_users_with_photos()
        if not users:
            Label(self.scrollable_frame, text="База данных пуста.").pack(pady=20)
            return
        for user_data in users:
            user_frame = Frame(self.scrollable_frame, borderwidth=1, relief="solid", padx=5, pady=5)
            try:
                image_stream = io.BytesIO(user_data['photo'])
                img = Image.open(image_stream).resize((100, 100), Image.Resampling.LANCZOS)
                photo_img = ImageTk.PhotoImage(img)
                photo_label = Label(user_frame, image=photo_img); photo_label.image = photo_img
                photo_label.grid(row=0, column=0, rowspan=3, padx=10, pady=5)
            except Exception as e:
                print(f"Ошибка загрузки фото для GUI: {e}")
                Label(user_frame, text="Нет фото", width=12, height=6).grid(row=0, column=0, rowspan=3, padx=10, pady=5)
            info_frame = Frame(user_frame)
            Label(info_frame, text=f"Имя: {user_data['first_name']}", font=("Arial", 10)).pack(anchor="w")
            Label(info_frame, text=f"Фамилия: {user_data['last_name']}", font=("Arial", 10)).pack(anchor="w")
            Label(info_frame, text=f"ID: {user_data['id']}", font=("Arial", 8)).pack(anchor="w")
            info_frame.grid(row=0, column=1, sticky="w", rowspan=2)
            buttons_frame = Frame(user_frame)
            Button(buttons_frame, text="Подробнее", command=lambda u_id=user_data['id']: self.show_user_details(u_id)).pack(side="left", padx=5)
            Button(buttons_frame, text="Редактировать", command=lambda u_id=user_data['id']: self.open_edit_user_window(u_id)).pack(side="left", padx=5)
            Button(buttons_frame, text="Удалить", fg="red", command=lambda u_id=user_data['id']: self.delete_user_action(u_id)).pack(side="left", padx=5)
            buttons_frame.grid(row=2, column=1, sticky="w", pady=5)
            user_frame.pack(fill="x", padx=10, pady=5, expand=True)

    def open_add_user_window(self):
        # ... (аналогично предыдущему коду)
        add_window = Toplevel(self.root); add_window.title("Добавить нового пользователя"); add_window.geometry("400x350"); add_window.transient(self.root); add_window.grab_set()
        self.new_user_photo_path = None
        Label(add_window, text="ID (табельный номер):").pack(pady=(10,0)); id_entry = Entry(add_window, width=40); id_entry.pack()
        Label(add_window, text="Имя:").pack(pady=(10,0)); first_name_entry = Entry(add_window, width=40); first_name_entry.pack()
        Label(add_window, text="Фамилия:").pack(pady=(10,0)); last_name_entry = Entry(add_window, width=40); last_name_entry.pack()
        Label(add_window, text="Номер паспорта (необязательно):").pack(pady=(10,0)); passport_entry = Entry(add_window, width=40); passport_entry.pack()
        Label(add_window, text="Отдел (необязательно):").pack(pady=(10,0)); departament_entry = Entry(add_window, width=40); departament_entry.pack()
        photo_path_label = Label(add_window, text="Фото не выбрано", fg="red"); photo_path_label.pack(pady=5)
        def _select_photo():
            path = filedialog.askopenfilename(title="Выберите фото", filetypes=[("Image files", "*.jpg *.jpeg *.png")])
            if path: self.new_user_photo_path = path; photo_path_label.config(text=os.path.basename(path), fg="green")
        Button(add_window, text="Выбрать фото...", command=_select_photo).pack()
        def _save_user():
            user_id, first_name, last_name, passport, departament = id_entry.get().strip(), first_name_entry.get().strip(), last_name_entry.get().strip(), passport_entry.get().strip(), departament_entry.get().strip()
            if not all([user_id, first_name, last_name, self.new_user_photo_path]): messagebox.showerror("Ошибка", "ID, Имя, Фамилия и Фото являются обязательными полями.", parent=add_window); return
            try:
                with open(self.new_user_photo_path, 'rb') as f: photo_data = f.read()
                if db.add_user(user_id, first_name, last_name, passport, departament, photo_data): messagebox.showinfo("Успех", "Новый пользователь успешно добавлен.", parent=add_window); add_window.destroy(); self.refresh_user_db_window()
                else: messagebox.showerror("Ошибка", f"Пользователь с ID '{user_id}' уже существует.", parent=add_window)
            except Exception as e: messagebox.showerror("Ошибка сохранения", f"Произошла ошибка: {e}", parent=add_window)
        Button(add_window, text="Сохранить пользователя", command=_save_user).pack(pady=10)

    def open_edit_user_window(self, user_id):
        user_to_edit = db.get_user_details(user_id)
        if not user_to_edit: messagebox.showerror("Ошибка", "Не удалось найти пользователя."); return
        edit_window = Toplevel(self.root); edit_window.title("Редактировать пользователя"); edit_window.geometry("400x300"); edit_window.transient(self.root); edit_window.grab_set()
        Label(edit_window, text=f"ID: {user_id} (не изменяется)").pack(pady=(10,0))
        Label(edit_window, text="Имя:").pack(); first_name_entry = Entry(edit_window, width=40); first_name_entry.insert(0, user_to_edit['first_name']); first_name_entry.pack()
        Label(edit_window, text="Фамилия:").pack(); last_name_entry = Entry(edit_window, width=40); last_name_entry.insert(0, user_to_edit['last_name']); last_name_entry.pack()
        Label(edit_window, text="Номер паспорта:").pack(); passport_entry = Entry(edit_window, width=40); passport_entry.insert(0, user_to_edit['passport_number'] or ""); passport_entry.pack()
        Label(edit_window, text="Отдел:").pack(); departament_entry = Entry(edit_window, width=40); departament_entry.insert(0, user_to_edit['departament'] or ""); departament_entry.pack()
        def _save_changes():
            first_name, last_name = first_name_entry.get().strip(), last_name_entry.get().strip()
            if not first_name or not last_name: messagebox.showerror("Ошибка", "Имя и Фамилия не могут быть пустыми.", parent=edit_window); return
            try: db.update_user(user_id, first_name, last_name, passport_entry.get().strip(), departament_entry.get().strip()); messagebox.showinfo("Успех", "Данные пользователя обновлены.", parent=edit_window); edit_window.destroy(); self.refresh_user_db_window()
            except Exception as e: messagebox.showerror("Ошибка", f"Произошла ошибка: {e}", parent=edit_window)
        Button(edit_window, text="Сохранить изменения", command=_save_changes).pack(pady=10)

    def show_user_details(self, user_id):
        user = db.get_user_details(user_id)
        details_text = f"ID: {user['id']}\nИмя: {user['first_name']}\nФамилия: {user['last_name']}\nПаспорт: {user['passport_number'] or 'Не указан'}\nОтдел: {user['departament'] or 'Не указан'}"
        messagebox.showinfo("Подробная информация", details_text)
        
    def delete_user_action(self, user_id):
        if messagebox.askyesno("Подтверждение", f"Вы уверены, что хотите удалить пользователя с ID {user_id}? Это действие необратимо."):
            db.delete_user(user_id); messagebox.showinfo("Успех", "Пользователь удален."); self.refresh_user_db_window()

    def refresh_user_db_window(self):
        self.populate_user_list(); self.load_known_users()

    # --- Методы управления помещениями и доступом ---
    def open_rooms_management_window(self):
        # ... (код без изменений) ...
        win = Toplevel(self.root); win.title("Управление помещениями и доступом"); win.geometry("800x600"); win.transient(self.root); win.grab_set()
        left_frame = Frame(win, width=250, relief="solid", borderwidth=1); left_frame.pack(side="left", fill="y", padx=5, pady=5)
        Label(left_frame, text="Помещения").pack(); self.rooms_listbox = Listbox(left_frame); self.rooms_listbox.pack(fill="both", expand=True)
        right_frame = Frame(win); right_frame.pack(side="right", fill="both", expand=True, padx=5, pady=5)
        details_frame = Frame(right_frame, relief="solid", borderwidth=1); details_frame.pack(fill="both", expand=True)
        self.details_label = Label(details_frame, text="Выберите помещение из списка", font=("Arial", 12)); self.details_label.pack(pady=10)
        self.cameras_frame = Frame(details_frame); self.access_frame = Frame(details_frame)
        def populate_rooms_list():
            self.rooms_listbox.delete(0, END); self.rooms_data = {room['name_rooms']: room['id_rooms'] for room in db.get_all_rooms()}
            for name in self.rooms_data.keys(): self.rooms_listbox.insert(END, name)
        def on_room_select(event):
            selection = event.widget.curselection()
            if not selection: return
            room_name = event.widget.get(selection[0]); room_id = self.rooms_data[room_name]
            for widget in self.cameras_frame.winfo_children(): widget.destroy()
            for widget in self.access_frame.winfo_children(): widget.destroy()
            self.details_label.config(text=f"Настройки для: {room_name} (ID: {room_id})")
            Label(self.cameras_frame, text="Привязанные камеры (IP-адреса):", font=("Arial", 10, "bold")).pack(anchor="w")
            cameras = db.get_cameras_for_room(room_id)
            if cameras:
                for ip in cameras: Label(self.cameras_frame, text=f"- {ip}").pack(anchor="w", padx=10)
            else: Label(self.cameras_frame, text="Нет привязанных камер.").pack(anchor="w", padx=10)
            def add_camera():
                ip = simpledialog.askstring("Добавить камеру", "Введите IP-адрес новой камеры:", parent=win)
                if ip: db.link_camera_to_room(ip, room_id); on_room_select(event)
            Button(self.cameras_frame, text="Привязать камеру...", command=add_camera).pack(pady=5); self.cameras_frame.pack(fill="x", padx=10, pady=10)
            Label(self.access_frame, text="Доступ разрешен для отделов:", font=("Arial", 10, "bold")).pack(anchor="w")
            rules = db.get_rules_for_room(room_id)
            if rules:
                for dep in rules:
                    rule_f = Frame(self.access_frame); Label(rule_f, text=f"- {dep}").pack(side="left"); Button(rule_f, text="X", fg="red", command=lambda d=dep: remove_rule(d)).pack(side="left", padx=5); rule_f.pack(anchor="w", padx=10)
            else: Label(self.access_frame, text="Нет правил доступа.").pack(anchor="w", padx=10)
            def remove_rule(departament): db.remove_access_rule(departament, room_id); on_room_select(event)
            def add_rule():
                dep = simpledialog.askstring("Добавить правило", "Введите название отдела:", parent=win)
                if dep: db.add_access_rule(dep.strip(), room_id); on_room_select(event)
            Button(self.access_frame, text="Добавить правило...", command=add_rule).pack(pady=5); self.access_frame.pack(fill="x", padx=10, pady=10)
        self.rooms_listbox.bind("<<ListboxSelect>>", on_room_select); populate_rooms_list()

    # --- Методы управления потоком и логами ---
    def load_camera_history(self):
        self.camera_history = {}
        try:
            if os.path.exists(self.HISTORY_FILE):
                with open(self.HISTORY_FILE, 'r', encoding='utf-8') as f: self.camera_history = json.load(f)
                for name in self.camera_history: self.history_listbox.insert(END, name)
        except Exception as e: print(f"Ошибка загрузки истории камер: {e}")

    def save_camera_history(self):
        try:
            with open(self.HISTORY_FILE, 'w', encoding='utf-8') as f: json.dump(self.camera_history, f, ensure_ascii=False, indent=4)
        except Exception as e: print(f"Ошибка сохранения истории камер: {e}")

    def on_history_select(self, event):
        selection = event.widget.curselection()
        if selection:
            index = selection[0]; name = event.widget.get(index); data = self.camera_history.get(name)
            if data:
                self.location_entry.delete(0, END); self.location_entry.insert(0, name)
                self.ip_entry.delete(0, END); self.ip_entry.insert(0, data['ip'])
                self.port_entry.delete(0, END); self.port_entry.insert(0, data['port'])
    
    def log_event(self, message, access_level): self.log_queue.put((message, access_level))

    def process_log_queue(self):
        try:
            while not self.log_queue.empty():
                message, access_level = self.log_queue.get_nowait()
                log_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self.log_widget.config(state="normal")
                self.log_widget.insert(END, f"{log_time} - {message}\n", (access_level,))
                self.log_widget.config(state="disabled"); self.log_widget.see(END)
        finally: self.root.after(100, self.process_log_queue)

    def start_stream(self):
        ip_address = self.ip_entry.get(); port = self.port_entry.get(); location = self.location_entry.get().strip()
        if not ip_address or not port or not port.isdigit() or not location:
            messagebox.showerror("Ошибка", "Название/Место, IP-адрес и порт должны быть корректно заполнены."); return
        self.recent_detections.clear()
        if location not in self.camera_history: self.history_listbox.insert(0, location)
        self.camera_history[location] = {"ip": ip_address, "port": port}; self.save_camera_history()
        rtsp_url = f"rtsp://admin:admin@{ip_address}:{port}"; self.is_running = True
        self.start_button.config(state="disabled"); self.stop_button.config(state="normal")
        self.rtsp_cap = RTSPVideoCapture(rtsp_url); self.rtsp_cap.start()
        self.video_thread = threading.Thread(target=self.video_loop, args=(location, ip_address), daemon=True)
        self.video_thread.start(); self.update_gui_frame()

    def stop_stream(self):
        self.is_running = False
        if self.rtsp_cap: self.rtsp_cap.stop()
        if self.video_thread: self.video_thread.join(timeout=1.0)
        while not self.frame_queue.empty():
            try: self.frame_queue.get_nowait()
            except queue.Empty: continue
        self.start_button.config(state="normal"); self.stop_button.config(state="disabled")
        self.video_label.config(image='', background="black"); self.video_label.image = None

    def on_closing(self):
        self.stop_stream(); self.frame_saver.stop(); self.root.destroy()

    def update_gui_frame(self):
        try:
            frame = self.frame_queue.get_nowait()
            img = Image.fromarray(frame); imgtk = ImageTk.PhotoImage(image=img)
            self.video_label.imgtk = imgtk; self.video_label.config(image=imgtk)
        except queue.Empty: pass
        if self.is_running: self.root.after(33, self.update_gui_frame)

    def video_loop(self, location_name, ip_address):
        SAVE_FOLDER = "detected_faces"; os.makedirs(SAVE_FOLDER, exist_ok=True)
        import face_recognition
        room_id = db.get_room_by_camera_ip(ip_address)
        if not room_id: print(f"ВНИМАНИЕ: Камера с IP {ip_address} не привязана к помещению.")
        else: print(f"Камера {ip_address} работает в помещении '{room_id}'")

        known_encodings = [user["encoding"] for user in self.known_users]
        user_data_by_index = {i: user for i, user in enumerate(self.known_users)}
        
        yunet_model_path = "face_detection_yunet_2023mar.onnx"
        if not os.path.exists(yunet_model_path): messagebox.showerror("Ошибка", f"Модель '{yunet_model_path}' не найдена!"); return
        face_detector, active_trackers, tracked_names = None, [], []
        re_recognition_interval, frame_count, DETECTION_COOLDOWN_SECONDS = 15, 0, 30

        while self.is_running:
            ret, frame = self.rtsp_cap.read()
            if not ret: time.sleep(0.1); continue
            
            boxes_for_drawing, names_for_drawing = [], []
            if frame_count % re_recognition_interval == 0:
                active_trackers, tracked_names = [], []
                if face_detector is None and frame is not None: height, width, _ = frame.shape; face_detector = cv2.FaceDetectorYN.create(yunet_model_path, "", (width, height))
                if face_detector is not None:
                    h, w, _ = frame.shape; face_detector.setInputSize((w, h)); _, detected_faces = face_detector.detect(frame)
                    if detected_faces is not None and self.known_users:
                        # --- НАЧАЛО БЛОКА ДЛЯ ОТЛАДКИ ---
                        print(f"[DEBUG] Найдено лиц на кадре: {len(detected_faces)}. Известных пользователей: {len(self.known_users)}")
                        # --- КОНЕЦ БЛОКА ДЛЯ ОТЛАДКИ ---

                        rgb_frame_proc = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        for face_data in detected_faces:
                            box = face_data[0:4].astype(np.int32); (x,y,w,h) = box
                            encodings = face_recognition.face_encodings(rgb_frame_proc, [(y, x + w, y + h, x)])
                            
                            # --- НАЧАЛО БЛОКА ДЛЯ ОТЛАДКИ ---
                            if not encodings:
                                print("[DEBUG] Не удалось создать кодировку для обнаруженного лица.")
                                continue
                            # --- КОНЕЦ БЛОКА ДЛЯ ОТЛАДКИ ---

                            matches = face_recognition.compare_faces(known_encodings, encodings[0], tolerance=0.5)
                            
                            # --- НАЧАЛО БЛОКА ДЛЯ ОТЛАДКИ ---
                            print(f"[DEBUG] Результат сравнения: {matches}")
                            # --- КОНЕЦ БЛОКА ДЛЯ ОТЛАДКИ ---
                            
                            name, user_departament = "Unknown", None
                            if True in matches:
                                user = user_data_by_index.get(matches.index(True))
                                if user: name, user_departament = f"{user['name']} (ID: {user['id']})", user['departament']
                            
                            access_granted = db.check_access(user_departament, room_id)
                            last_seen = self.recent_detections.get((name, location_name))
                            if last_seen and (time.time() - last_seen) < DETECTION_COOLDOWN_SECONDS: continue
                            self.recent_detections[(name, location_name)] = time.time()
                            
                            event_msg = f"Обнаружен '{name}' в '{location_name}'."
                            log_level = "denied"
                            if access_granted: event_msg += " Доступ разрешен."; log_level = "granted"
                            elif name != "Unknown": event_msg += f" Доступ в '{room_id}' для отдела '{user_departament}' запрещен."
                            else: event_msg += " Доступ запрещен (неопознан)."
                            self.log_event(event_msg, log_level)
                            
                            # Сохранение кадра и добавление трекера
                            frame_to_save = frame.copy(); cv2.rectangle(frame_to_save, (x, y), (x + w, y + h), (0, 0, 255), 2)
                            cv2.putText(frame_to_save, name, (x, y - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
                            safe_name = name.replace(" ", "_").replace(":", "-").replace("(", "").replace(")", "")
                            filename = f"{SAVE_FOLDER}/{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}_{safe_name}.jpg"
                            self.frame_saver.save(filename, frame_to_save)
                            tracker = cv2.TrackerCSRT_create(); tracker.init(frame, tuple(box)); active_trackers.append(tracker); tracked_names.append(name)
            else:
                new_trackers, new_names = [], []
                for i, tracker in enumerate(active_trackers):
                    success, box = tracker.update(frame)
                    if success: new_trackers.append(tracker); new_names.append(tracked_names[i]); boxes_for_drawing.append(box); names_for_drawing.append(tracked_names[i])
                active_trackers, tracked_names = new_trackers, new_names
            
            frame_count += 1
            for box, name in zip(boxes_for_drawing, names_for_drawing):
                (x, y, w, h) = [int(v) for v in box]
                cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
                cv2.putText(frame, name, (x + 6, y + h - 6), cv2.FONT_HERSHEY_DUPLEX, 0.6, (255, 255, 255), 1)
            
            try:
                if self.frame_queue.full(): self.frame_queue.get_nowait()
                self.frame_queue.put_nowait(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            except queue.Full: pass
            except Exception as e: print(f"Ошибка в видеопотоке: {e}")

# --- Точка входа в программу ---
if __name__ == "__main__":
    if shutil.which("cmake") is None: messagebox.showerror("Критическая ошибка", "CMake не найден. Установите его и добавьте в PATH.")
    else: root = Tk(); app = App(root); root.mainloop()