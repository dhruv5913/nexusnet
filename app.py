import qrcode
import base64
import uuid
import random
import string
from io import BytesIO
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room, close_room

app = Flask(__name__)
app.config['SECRET_KEY'] = 'super_secret_nexus_key' 
# cors_allowed_origins="*" is strictly required for cloud hosting
socketio = SocketIO(app, max_http_buffer_size=10 * 1024 * 1024, cors_allowed_origins="*")

# Database to track rooms and users
active_rooms = {}
user_room_map = {} 

def generate_room_code(length=5):
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=length))
        if code not in active_rooms:
            return code

@app.route('/')
def index():
    # Automatically grabs your live Render domain (e.g., https://your-app.onrender.com)
    app_url = request.url_root.rstrip('/') 
    
    # Generate the QR Code for the Lobby
    qr = qrcode.QRCode(version=1, box_size=4, border=2)
    qr.add_data(app_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    qr_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")

    return render_template('index.html', app_url=app_url, qr_code=qr_base64)

# --- ROOM LOGIC ---

@socketio.on('create_room')
def handle_create_room(data):
    username = data['username']
    room_code = generate_room_code()
    
    active_rooms[room_code] = {
        "creator": request.sid,
        "users": {request.sid: username}
    }
    user_room_map[request.sid] = room_code
    join_room(room_code)
    
    emit('room_joined', {'room_code': room_code, 'users': list(active_rooms[room_code]['users'].values())})
    emit('receive_message', {'user': 'System', 'text': f'Room created successfully! Share code: {room_code}'}, to=room_code)

@socketio.on('join_room')
def handle_join_room(data):
    username = data['username']
    room_code = data['room_code'].upper()

    if room_code not in active_rooms:
        emit('error_message', {'message': 'Invalid Room Code. It may have been closed.'})
        return

    if username in active_rooms[room_code]['users'].values():
        emit('error_message', {'message': 'Username already taken in this room.'})
        return

    active_rooms[room_code]['users'][request.sid] = username
    user_room_map[request.sid] = room_code
    join_room(room_code)
    
    emit('room_joined', {'room_code': room_code, 'users': list(active_rooms[room_code]['users'].values())})
    emit('update_users', list(active_rooms[room_code]['users'].values()), to=room_code)
    emit('receive_message', {'user': 'System', 'text': f'{username} has joined the chat!'}, to=room_code)

@socketio.on('disconnect')
def handle_disconnect():
    room_code = user_room_map.get(request.sid)
    
    if room_code and room_code in active_rooms:
        username = active_rooms[room_code]['users'].get(request.sid)
        
        # SELF DESTRUCT: If the creator leaves, destroy the room
        if active_rooms[room_code]['creator'] == request.sid:
            emit('room_terminated', to=room_code) 
            close_room(room_code) 
            del active_rooms[room_code] 
        else:
            if request.sid in active_rooms[room_code]['users']:
                del active_rooms[room_code]['users'][request.sid]
            emit('update_users', list(active_rooms[room_code]['users'].values()), to=room_code)
            emit('receive_message', {'user': 'System', 'text': f'{username} has disconnected.'}, to=room_code)
        
        del user_room_map[request.sid]

# --- CHAT & WEBRTC LOGIC ---

@socketio.on('send_message')
def handle_message(data):
    room_code = user_room_map.get(request.sid)
    if room_code:
        data['id'] = str(uuid.uuid4())
        emit('receive_message', data, to=room_code)

@socketio.on('private_message')
def handle_private_message(data):
    room_code = user_room_map.get(request.sid)
    if not room_code: return

    target_username = data.get('target')
    data['id'] = str(uuid.uuid4())
    data['is_private'] = True 
    
    target_sid = None
    for sid, uname in active_rooms[room_code]['users'].items():
        if uname == target_username:
            target_sid = sid
            break

    if target_sid:
        emit('receive_message', data, room=target_sid)
        emit('receive_message', data, room=request.sid)
    else:
        emit('receive_message', {'user': 'System', 'text': f"Could not deliver. {target_username} is no longer online."}, room=request.sid)

@socketio.on('webrtc_signal')
def handle_webrtc_signal(data):
    room_code = user_room_map.get(request.sid)
    if not room_code: return

    target_username = data.get('target')
    target_sid = None
    
    for sid, uname in active_rooms[room_code]['users'].items():
        if uname == target_username:
            target_sid = sid
            break

    if target_sid:
        emit('webrtc_signal', data, room=target_sid)

@socketio.on('delete_message')
def handle_delete(data):
    room_code = user_room_map.get(request.sid)
    if room_code:
        emit('message_deleted', data, to=room_code)

# Notice there is no app.run() here! Cloud providers use a different engine to start the file.