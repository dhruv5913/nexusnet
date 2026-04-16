# app.py
import qrcode
import base64
import uuid
import random
import string
import os
from io import BytesIO
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room, close_room

app = Flask(__name__)
# Use environment variable for secret key in production
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'super_secret_nexus_key')

# Render.com specific: Use eventlet for WebSocket support
socketio = SocketIO(
    app, 
    max_http_buffer_size=10 * 1024 * 1024, 
    cors_allowed_origins="*",
    async_mode='eventlet',
    ping_timeout=60,
    ping_interval=25,
    logger=True,
    engineio_logger=True
)

# Database to track rooms, users, and active group calls
active_rooms = {}
user_room_map = {} 
active_group_calls = {}

# MAX participants in group call (including host)
MAX_GROUP_CALL_PARTICIPANTS = 4

def generate_room_code(length=5):
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=length))
        if code not in active_rooms:
            return code

@app.route('/')
def index():
    # Render automatically provides HTTPS URL
    app_url = request.url_root.rstrip('/')
    
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
    username = data.get('username', 'Unknown')
    room_code = generate_room_code()
    
    active_rooms[room_code] = {
        "creator": request.sid,
        "creator_name": username,
        "users": {request.sid: username}
    }
    user_room_map[request.sid] = room_code
    join_room(room_code)
    
    emit('room_joined', {
        'room_code': room_code, 
        'users': list(active_rooms[room_code]['users'].values()),
        'is_host': True,
        'host_name': username,
        'host_sid': request.sid
    })
    emit('receive_message', {
        'user': 'System', 
        'text': f'Room created successfully! Share code: {room_code}',
        'type': 'system'
    }, to=room_code)

@socketio.on('join_room')
def handle_join_room(data):
    username = data.get('username', 'Unknown')
    room_code = data.get('room_code', '').upper()

    if room_code not in active_rooms:
        emit('error_message', {'message': 'Invalid Room Code. It may have been closed.'})
        return

    if username in active_rooms[room_code]['users'].values():
        emit('error_message', {'message': 'Username already taken in this room.'})
        return

    active_rooms[room_code]['users'][request.sid] = username
    user_room_map[request.sid] = room_code
    join_room(room_code)
    
    is_host = active_rooms[room_code]['creator'] == request.sid
    
    emit('room_joined', {
        'room_code': room_code, 
        'users': list(active_rooms[room_code]['users'].values()),
        'is_host': is_host,
        'host_name': active_rooms[room_code]['creator_name'],
        'host_sid': active_rooms[room_code]['creator']
    })
    
    emit('update_users', {
        'users': list(active_rooms[room_code]['users'].values()),
        'host_sid': active_rooms[room_code]['creator']
    }, to=room_code)
    
    emit('receive_message', {
        'user': 'System', 
        'text': f'{username} has joined the chat!',
        'type': 'system'
    }, to=room_code)

@socketio.on('disconnect')
def handle_disconnect():
    room_code = user_room_map.get(request.sid)
    
    if room_code and room_code in active_rooms:
        username = active_rooms[room_code]['users'].get(request.sid, 'Unknown')
        
        # Clean up group call participation
        if room_code in active_group_calls:
            if request.sid in active_group_calls[room_code]['participants']:
                active_group_calls[room_code]['participants'].remove(request.sid)
                emit('group_call_user_left', {'sid': request.sid, 'username': username}, to=room_code)
                if len(active_group_calls[room_code]['participants']) == 0:
                    del active_group_calls[room_code]
        
        # SELF DESTRUCT: If the creator leaves, destroy the room
        if active_rooms[room_code]['creator'] == request.sid:
            emit('room_terminated', to=room_code) 
            close_room(room_code) 
            del active_rooms[room_code]
            if room_code in active_group_calls:
                del active_group_calls[room_code]
        else:
            if request.sid in active_rooms[room_code]['users']:
                del active_rooms[room_code]['users'][request.sid]
            emit('update_users', {
                'users': list(active_rooms[room_code]['users'].values()),
                'host_sid': active_rooms[room_code]['creator']
            }, to=room_code)
            emit('receive_message', {
                'user': 'System', 
                'text': f'{username} has disconnected.',
                'type': 'system'
            }, to=room_code)
        
        if request.sid in user_room_map:
            del user_room_map[request.sid]

# --- HOST CONTROLS: KICK USER ---

@socketio.on('kick_user')
def handle_kick_user(data):
    room_code = user_room_map.get(request.sid)
    if not room_code or room_code not in active_rooms:
        return
    
    # Verify host
    if active_rooms[room_code]['creator'] != request.sid:
        emit('error_message', {'message': 'Only the host can remove users.'})
        return
    
    target_username = data.get('username')
    target_sid = None
    
    for sid, uname in active_rooms[room_code]['users'].items():
        if uname == target_username:
            target_sid = sid
            break
    
    if target_sid:
        # If target is in group call, remove them first
        if room_code in active_group_calls and target_sid in active_group_calls[room_code]['participants']:
            active_group_calls[room_code]['participants'].remove(target_sid)
            emit('group_call_user_removed', {'sid': target_sid, 'username': target_username}, to=room_code)
        
        emit('kicked', {'message': 'You have been removed from the room by the host.'}, to=target_sid)
        leave_room(room_code, sid=target_sid)
        
        if target_sid in active_rooms[room_code]['users']:
            del active_rooms[room_code]['users'][target_sid]
        
        if target_sid in user_room_map:
            del user_room_map[target_sid]
        
        emit('receive_message', {
            'user': 'System',
            'text': f'{target_username} was removed by the host.',
            'type': 'system'
        }, to=room_code)
        
        emit('update_users', {
            'users': list(active_rooms[room_code]['users'].values()),
            'host_sid': active_rooms[room_code]['creator']
        }, to=room_code)

# --- GROUP CALL LOGIC ---

@socketio.on('start_group_call')
def handle_start_group_call(data):
    room_code = user_room_map.get(request.sid)
    if not room_code or room_code not in active_rooms:
        return
    
    # Only host can start group calls
    if active_rooms[room_code]['creator'] != request.sid:
        return
    
    call_type = data.get('call_type', 'video')
    
    active_group_calls[room_code] = {
        'type': call_type,
        'participants': [request.sid],
        'host': request.sid,
        'max_participants': MAX_GROUP_CALL_PARTICIPANTS
    }
    
    emit('group_call_started', {
        'call_type': call_type,
        'host_sid': request.sid,
        'host_name': active_rooms[room_code]['creator_name'],
        'max_participants': MAX_GROUP_CALL_PARTICIPANTS
    }, to=room_code)

@socketio.on('join_group_call')
def handle_join_group_call(data):
    room_code = user_room_map.get(request.sid)
    if not room_code or room_code not in active_group_calls:
        return
    
    # Check if call is full
    current_participants = active_group_calls[room_code]['participants']
    if len(current_participants) >= active_group_calls[room_code]['max_participants']:
        emit('group_call_full', {
            'message': f'Group call is full (max {active_group_calls[room_code]["max_participants"]} participants)',
            'current_participants': len(current_participants)
        })
        return
    
    if request.sid not in current_participants:
        active_group_calls[room_code]['participants'].append(request.sid)
    
    # Notify existing participants to create offers for this new peer (mesh topology)
    for participant_sid in active_group_calls[room_code]['participants']:
        if participant_sid != request.sid:
            emit('group_call_new_peer', {
                'new_peer_sid': request.sid,
                'new_peer_name': active_rooms[room_code]['users'].get(request.sid, 'Unknown')
            }, to=participant_sid)
    
    emit('group_call_joined', {
        'participants': [
            {'sid': sid, 'name': active_rooms[room_code]['users'].get(sid, 'Unknown')}
            for sid in active_group_calls[room_code]['participants']
            if sid != request.sid
        ],
        'call_type': active_group_calls[room_code]['type'],
        'max_participants': active_group_calls[room_code]['max_participants']
    }, to=request.sid)

@socketio.on('leave_group_call')
def handle_leave_group_call(data):
    room_code = user_room_map.get(request.sid)
    if not room_code or room_code not in active_group_calls:
        return
    
    if request.sid in active_group_calls[room_code]['participants']:
        active_group_calls[room_code]['participants'].remove(request.sid)
        username = active_rooms[room_code]['users'].get(request.sid, 'Unknown')
        emit('group_call_user_left', {'sid': request.sid, 'username': username}, to=room_code)
        
        if len(active_group_calls[room_code]['participants']) == 0:
            del active_group_calls[room_code]

@socketio.on('end_group_call')
def handle_end_group_call(data):
    room_code = user_room_map.get(request.sid)
    if not room_code or room_code not in active_group_calls:
        return
    
    # Only host can end the group call
    if active_group_calls[room_code]['host'] != request.sid:
        return
    
    emit('group_call_ended', {'by': 'host'}, to=room_code)
    del active_group_calls[room_code]

@socketio.on('remove_from_group_call')
def handle_remove_from_group_call(data):
    """Host can remove a specific user from the group call without kicking from room"""
    room_code = user_room_map.get(request.sid)
    if not room_code or room_code not in active_group_calls:
        return
    
    # Only host can remove participants
    if active_group_calls[room_code]['host'] != request.sid:
        emit('error_message', {'message': 'Only the host can remove participants from the call.'})
        return
    
    target_sid = data.get('target_sid')
    target_username = data.get('target_username')
    
    if target_sid and target_sid in active_group_calls[room_code]['participants']:
        active_group_calls[room_code]['participants'].remove(target_sid)
        emit('removed_from_call', {'message': 'You have been removed from the call by the host.'}, to=target_sid)
        emit('group_call_user_left', {'sid': target_sid, 'username': target_username}, to=room_code)

@socketio.on('group_webrtc_signal')
def handle_group_webrtc_signal(data):
    room_code = user_room_map.get(request.sid)
    if not room_code or room_code not in active_group_calls:
        return
    
    target_sid = data.get('target_sid')
    if target_sid and target_sid in active_group_calls[room_code]['participants']:
        emit('group_webrtc_signal', {
            'sender_sid': request.sid,
            'sender_name': active_rooms[room_code]['users'].get(request.sid, 'Unknown'),
            'signal': data.get('signal'),
            'type': data.get('type')
        }, to=target_sid)

# --- CHAT & PRIVATE WEBRTC LOGIC ---

@socketio.on('send_message')
def handle_message(data):
    room_code = user_room_map.get(request.sid)
    if room_code:
        data['id'] = str(uuid.uuid4())
        data['timestamp'] = data.get('timestamp', '')
        emit('receive_message', data, to=room_code)

@socketio.on('private_message')
def handle_private_message(data):
    room_code = user_room_map.get(request.sid)
    if not room_code: 
        return

    target_username = data.get('target')
    data['id'] = str(uuid.uuid4())
    data['is_private'] = True 
    data['timestamp'] = data.get('timestamp', '')
    
    target_sid = None
    for sid, uname in active_rooms[room_code]['users'].items():
        if uname == target_username:
            target_sid = sid
            break

    if target_sid:
        emit('receive_message', data, room=target_sid)
        emit('receive_message', data, room=request.sid)
    else:
        emit('receive_message', {
            'user': 'System', 
            'text': f"Could not deliver. {target_username} is no longer online.",
            'type': 'system'
        }, room=request.sid)

@socketio.on('webrtc_signal')
def handle_webrtc_signal(data):
    room_code = user_room_map.get(request.sid)
    if not room_code: 
        return

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

# Render.com requires this specific setup
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
