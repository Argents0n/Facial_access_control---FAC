import json
import os
import shutil
import io
import face_recognition

# --- Константы для имен файлов ---
USERS_FILE = 'users.json'
PHOTOS_DIR = 'user_photos' # Папка для хранения фотографий пользователей
ROOMS_FILE = 'rooms.json'
CAMERAS_FILE = 'cameras.json'
ACCESS_RULES_FILE = 'access_rules.json'

# --- Хранилища данных в памяти ---
data_storage = {
    'users': [],
    'rooms': [],
    'cameras': [],
    'access_rules': []
}

def _load_json(filename):
    """Загружает данные из JSON-файла."""
    if not os.path.exists(filename):
        return []
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Ошибка при чтении файла {filename}: {e}")
        return []

def _save_json(filename, data):
    """Сохраняет данные в JSON-файл."""
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except IOError as e:
        print(f"Ошибка при сохранении файла {filename}: {e}")

def initialize_database():
    """Инициализирует 'базу данных' путем загрузки всех JSON-файлов в память."""
    data_storage['users'] = _load_json(USERS_FILE)
    data_storage['rooms'] = _load_json(ROOMS_FILE)
    data_storage['cameras'] = _load_json(CAMERAS_FILE)
    data_storage['access_rules'] = _load_json(ACCESS_RULES_FILE)
    
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    
    print("Данные из JSON-файлов загружены.")

# --- Функции для Пользователей (без изменений) ---

def add_user(user_id, first_name, last_name, passport, departament, photo_data):
    if any(u['id'] == user_id for u in data_storage['users']):
         # В исходном коде использовался photo_path, но GUI передает photo_data.
         # Приводим к единому виду для согласованности.
         return False
    
    new_user = {
        "id": user_id, "first_name": first_name, "last_name": last_name,
        "passport_number": passport, "departament": departament
    }
    data_storage['users'].append(new_user)

    # Логика сохранения фото из GUI (требует бинарных данных)
    try:
        image = Image.open(io.BytesIO(photo_data))
        # Ищем существующее фото, чтобы определить расширение, или сохраняем как jpg
        photo_filename = f"{user_id}.jpg" # Упрощаем до jpg
        image.save(os.path.join(PHOTOS_DIR, photo_filename))
    except Exception as e:
        print(f"Ошибка сохранения фото для пользователя {user_id}: {e}")
        # Если фото не удалось сохранить, пользователя все равно можно добавить
        pass

    _save_json(USERS_FILE, data_storage['users'])
    return True

def get_all_users_with_photos():
    users_with_photos = []
    for user in data_storage['users']:
        user_copy = user.copy()
        photo_path = next((os.path.join(PHOTOS_DIR, f) for f in os.listdir(PHOTOS_DIR) if f.startswith(str(user['id']))), None)
        if photo_path and os.path.exists(photo_path):
             with open(photo_path, 'rb') as f:
                 user_copy['photo'] = f.read()
        else:
            user_copy['photo'] = None
        users_with_photos.append(user_copy)
    return users_with_photos


def get_user_details(user_id):
    return next((user for user in data_storage['users'] if str(user['id']) == str(user_id)), None)


def update_user(user_id, first_name, last_name, passport, departament):
    user = get_user_details(user_id)
    if user:
        user['first_name'] = first_name
        user['last_name'] = last_name
        user['passport_number'] = passport
        user['departament'] = departament
        _save_json(USERS_FILE, data_storage['users'])

def delete_user(user_id):
    data_storage['users'] = [u for u in data_storage['users'] if str(u['id']) != str(user_id)]
    photo_path = next((os.path.join(PHOTOS_DIR, f) for f in os.listdir(PHOTOS_DIR) if f.startswith(str(user_id))), None)
    if photo_path and os.path.exists(photo_path):
        os.remove(photo_path)
    _save_json(USERS_FILE, data_storage['users'])

def get_known_face_encodings():
    known_users = []
    all_users = get_all_users_with_photos()
    for user in all_users:
        if not user.get('photo'):
            continue
        try:
            image_stream = io.BytesIO(user['photo'])
            image = face_recognition.load_image_file(image_stream)
            encodings = face_recognition.face_encodings(image)
            if encodings:
                known_users.append({
                    "name": f"{user['first_name']} {user['last_name']}",
                    "id": user['id'], "departament": user['departament'], "encoding": encodings[0]
                })
        except Exception as e:
            print(f"Ошибка обработки фото для пользователя ID {user['id']}: {e}")
    return known_users

# --- Функции для Помещений и Доступа (без изменений) ---

def get_all_rooms():
    return sorted(data_storage['rooms'], key=lambda x: x['name_rooms'])

def get_rules_for_room(room_id):
    return [rule['departament'] for rule in data_storage['access_rules'] if rule['id_rooms'] == room_id]

def add_access_rule(departament, room_id):
    rule = {'departament': departament, 'id_rooms': room_id}
    if rule not in data_storage['access_rules']:
        data_storage['access_rules'].append(rule)
        _save_json(ACCESS_RULES_FILE, data_storage['access_rules'])

def remove_access_rule(departament, room_id):
    data_storage['access_rules'] = [r for r in data_storage['access_rules'] if not (r['departament'] == departament and r['id_rooms'] == room_id)]
    _save_json(ACCESS_RULES_FILE, data_storage['access_rules'])

def check_access(departament, room_id):
    if not departament or not room_id:
        return False
    rules = get_rules_for_room(room_id)
    return departament in rules

# --- ИСПРАВЛЕННЫЕ Функции для Камер ---

def get_room_by_camera_ip(camera_ip):
    """[НОВАЯ ФУНКЦИЯ] Находит ID помещения, к которому привязана камера по IP."""
    camera = next((cam for cam in data_storage['cameras'] if cam['camera_ip'] == camera_ip), None)
    return camera['id_rooms'] if camera else None

def get_all_cameras_with_rooms():
    """Возвращает список всех камер с их IP и названием привязанного помещения."""
    cameras_info = []
    rooms_map = {room['id_rooms']: room['name_rooms'] for room in data_storage['rooms']}
    for cam in data_storage['cameras']:
        cameras_info.append({
            'camera_ip': cam['camera_ip'], # ИЗМЕНЕНО: camera_id -> camera_ip
            'id_rooms': cam['id_rooms'],
            'name_rooms': rooms_map.get(cam['id_rooms'])
        })
    return sorted(cameras_info, key=lambda x: x['camera_ip'])

def get_cameras_for_room(room_id):
    return [cam['camera_ip'] for cam in data_storage['cameras'] if cam['id_rooms'] == room_id] # ИЗМЕНЕНО: camera_id -> camera_ip
    
def link_camera_to_room(camera_ip, room_id):
    """Привязывает или обновляет привязку камеры к помещению по IP."""
    camera = next((cam for cam in data_storage['cameras'] if cam['camera_ip'] == camera_ip), None) # ИЗМЕНЕНО: camera_id -> camera_ip
    if camera:
        camera['id_rooms'] = room_id
    else:
        data_storage['cameras'].append({'camera_ip': camera_ip, 'id_rooms': room_id}) # ИЗМЕНЕНО: camera_id -> camera_ip
    _save_json(CAMERAS_FILE, data_storage['cameras'])

def update_camera(old_ip, new_ip, new_room_id):
    """Обновляет IP камеры и/или ее привязку к помещению."""
    camera = next((cam for cam in data_storage['cameras'] if cam['camera_ip'] == old_ip), None) # ИЗМЕНЕНО: camera_id -> camera_ip
    if camera:
        camera['camera_ip'] = new_ip # ИЗМЕНЕНО: camera_id -> camera_ip
        camera['id_rooms'] = new_room_id
        _save_json(CAMERAS_FILE, data_storage['cameras'])

def delete_camera(camera_ip):
    """Удаляет камеру по IP."""
    data_storage['cameras'] = [cam for cam in data_storage['cameras'] if cam['camera_ip'] != camera_ip] # ИЗМЕНЕНО: camera_id -> camera_ip
    _save_json(CAMERAS_FILE, data_storage['cameras'])