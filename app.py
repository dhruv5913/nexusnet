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
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'super_secret_nexus_key')

socketio = SocketIO(
    app,
    max_http_buffer_size=10 * 1024 * 1024,
    cors_allowed_origins="*",
    async_mode='eventlet',
    ping_timeout=90,
    ping_interval=30,
    logger=True,
    engineio_logger=True
)

active_rooms = {}
user_room_map = {}
active_group_calls = {}
MAX_GROUP_CALL_PARTICIPANTS = 4

def generate_room_code(length=5):
    """Generate a numeric-only room code."""
    while True:
        code = "".join(random.choices(string.digits, k=length))
        if code not in active_rooms:
            return code

@app.route('/')
def index():
    app_url = request.url_root.rstrip('/')
    qr = qrcode.QRCode(version=1, box_size=4, border=2)
    qr.add_data(app_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    qr_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return render_template('index.html', app_url=app_url, qr_code=qr_base64)

# ---------- HEARTBEAT ----------
@socketio.on('ping')
def handle_ping():
    emit('pong')

# ---------- ROOM LOGIC ----------
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
        if room_code in active_group_calls and request.sid in active_group_calls[room_code]['participants']:
            active_group_calls[room_code]['participants'].remove(request.sid)
            emit('user_left_group_call', {
                'sid': request.sid,
                'username': username,
                'participant_count': len(active_group_calls[room_code]['participants'])
            }, to=room_code)
            if len(active_group_calls[room_code]['participants']) == 0:
                del active_group_calls[room_code]
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

# ---------- KICK USER ----------
@socketio.on('kick_user')
def handle_kick_user(data):
    room_code = user_room_map.get(request.sid)
    if not room_code or room_code not in active_rooms:
        return
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
        if room_code in active_group_calls and target_sid in active_group_calls[room_code]['participants']:
            active_group_calls[room_code]['participants'].remove(target_sid)
            emit('user_left_group_call', {'sid': target_sid, 'username': target_username}, to=room_code)
        emit('kicked', {'message': 'You have been removed from the room by the host.'}, to=target_sid)
        leave_room(room_code, sid=target_sid)
        if target_sid in active_rooms[room_code]['users']:
            del active_rooms[room_code]['users'][target_sid]
        if target_sid in user_room_map:
            del user_room_map[target_sid]
        emit('receive_message', {'user': 'System', 'text': f'{target_username} was removed by the host.'}, to=room_code)
        emit('update_users', {
            'users': list(active_rooms[room_code]['users'].values()),
            'host_sid': active_rooms[room_code]['creator']
        }, to=room_code)

# ---------- PRIVATE CALL SIGNALING ----------
@socketio.on('call_request')
def handle_call_request(data):
    room_code = user_room_map.get(request.sid)
    if not room_code:
        return
    target_name = data.get('target')
    target_sid = None
    for sid, name in active_rooms[room_code]['users'].items():
        if name == target_name:
            target_sid = sid
            break
    if target_sid:
        emit('incoming_call', {
            'caller': active_rooms[room_code]['users'][request.sid],
            'type': data.get('type')
        }, to=target_sid)

@socketio.on('call_accept')
def handle_call_accept(data):
    room_code = user_room_map.get(request.sid)
    if not room_code:
        return
    target_name = data.get('target')
    target_sid = None
    for sid, name in active_rooms[room_code]['users'].items():
        if name == target_name:
            target_sid = sid
            break
    if target_sid:
        emit('call_accepted', {'target': active_rooms[room_code]['users'][request.sid]}, to=target_sid)

@socketio.on('call_reject')
def handle_call_reject(data):
    room_code = user_room_map.get(request.sid)
    if not room_code:
        return
    target_name = data.get('target')
    target_sid = None
    for sid, name in active_rooms[room_code]['users'].items():
        if name == target_name:
            target_sid = sid
            break
    if target_sid:
        emit('call_rejected', {'message': f'{active_rooms[room_code]["users"][request.sid]} rejected the call.'}, to=target_sid)

@socketio.on('call_end')
def handle_call_end(data):
    room_code = user_room_map.get(request.sid)
    if not room_code:
        return
    target_name = data.get('target')
    target_sid = None
    for sid, name in active_rooms[room_code]['users'].items():
        if name == target_name:
            target_sid = sid
            break
    if target_sid:
        emit('call_ended', {}, to=target_sid)

@socketio.on('offer')
def handle_offer(data):
    room_code = user_room_map.get(request.sid)
    if not room_code:
        return
    target_name = data.get('target')
    target_sid = None
    for sid, name in active_rooms[room_code]['users'].items():
        if name == target_name:
            target_sid = sid
            break
    if target_sid:
        emit('offer', {'offer': data['offer'], 'caller': active_rooms[room_code]['users'][request.sid]}, to=target_sid)

@socketio.on('answer')
def handle_answer(data):
    room_code = user_room_map.get(request.sid)
    if not room_code:
        return
    target_name = data.get('target')
    target_sid = None
    for sid, name in active_rooms[room_code]['users'].items():
        if name == target_name:
            target_sid = sid
            break
    if target_sid:
        emit('answer', {'answer': data['answer']}, to=target_sid)

@socketio.on('ice_candidate')
def handle_ice_candidate(data):
    room_code = user_room_map.get(request.sid)
    if not room_code:
        return
    target_name = data.get('target')
    target_sid = None
    for sid, name in active_rooms[room_code]['users'].items():
        if name == target_name:
            target_sid = sid
            break
    if target_sid:
        emit('ice_candidate', {'candidate': data['candidate']}, to=target_sid)

# ---------- GROUP CALL ----------
@socketio.on('start_group_call')
def handle_start_group_call(data):
    room_code = user_room_map.get(request.sid)
    if not room_code or room_code not in active_rooms:
        return
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
        'type': call_type,
        'host_sid': request.sid,
        'host_name': active_rooms[room_code]['creator_name'],
        'max_participants': MAX_GROUP_CALL_PARTICIPANTS
    }, to=room_code)

@socketio.on('join_group_call')
def handle_join_group_call(data):
    room_code = user_room_map.get(request.sid)
    if not room_code or room_code not in active_group_calls:
        return
    call = active_group_calls[room_code]
    if len(call['participants']) >= call['max_participants']:
        emit('group_call_full', {'message': f'Group call is full (max {call["max_participants"]} participants)'})
        return
    if request.sid not in call['participants']:
        call['participants'].append(request.sid)
    for sid in call['participants']:
        if sid != request.sid:
            emit('group_call_new_peer', {
                'peer_sid': request.sid,
                'peer_name': active_rooms[room_code]['users'][request.sid]
            }, to=sid)
    participants_list = [
        {'sid': sid, 'name': active_rooms[room_code]['users'][sid]}
        for sid in call['participants'] if sid != request.sid
    ]
    emit('group_call_joined', {
        'participants': participants_list,
        'call_type': call['type']
    }, to=request.sid)
    emit('user_joined_group_call', {
        'participant_count': len(call['participants']),
        'max_participants': call['max_participants']
    }, to=room_code)

@socketio.on('leave_group_call')
def handle_leave_group_call():
    room_code = user_room_map.get(request.sid)
    if not room_code or room_code not in active_group_calls:
        return
    if request.sid in active_group_calls[room_code]['participants']:
        active_group_calls[room_code]['participants'].remove(request.sid)
        username = active_rooms[room_code]['users'].get(request.sid, 'Unknown')
        emit('user_left_group_call', {
            'sid': request.sid,
            'username': username,
            'participant_count': len(active_group_calls[room_code]['participants'])
        }, to=room_code)
        if len(active_group_calls[room_code]['participants']) == 0:
            del active_group_calls[room_code]

@socketio.on('remove_from_group_call')
def handle_remove_from_group_call(data):
    room_code = user_room_map.get(request.sid)
    if not room_code or room_code not in active_group_calls:
        return
    if active_group_calls[room_code]['host'] != request.sid:
        return
    target_sid = data.get('target_sid')
    if target_sid and target_sid in active_group_calls[room_code]['participants']:
        active_group_calls[room_code]['participants'].remove(target_sid)
        username = active_rooms[room_code]['users'].get(target_sid, 'Unknown')
        emit('user_left_group_call', {
            'sid': target_sid,
            'username': username,
            'participant_count': len(active_group_calls[room_code]['participants'])
        }, to=room_code)
        emit('kicked_from_call', {'message': 'You were removed from the group call by the host.'}, to=target_sid)
        if len(active_group_calls[room_code]['participants']) == 0:
            del active_group_calls[room_code]

@socketio.on('end_group_call')
def handle_end_group_call():
    room_code = user_room_map.get(request.sid)
    if not room_code or room_code not in active_group_calls:
        return
    if active_group_calls[room_code]['host'] != request.sid:
        return
    emit('group_call_ended', {}, to=room_code)
    del active_group_calls[room_code]

@socketio.on('group_webrtc_signal')
def handle_group_webrtc_signal(data):
    room_code = user_room_map.get(request.sid)
    if not room_code or room_code not in active_group_calls:
        return
    target_sid = data.get('target_sid')
    if target_sid and target_sid in active_group_calls[room_code]['participants']:
        emit('group_webrtc_signal', {
            'sender_sid': request.sid,
            'sender_name': active_rooms[room_code]['users'][request.sid],
            'signal': data['signal'],
            'type': data['type']
        }, to=target_sid)

# ---------- CHAT MESSAGES ----------
@socketio.on('send_message')
def handle_message(data):
    room_code = user_room_map.get(request.sid)
    if room_code:
        data['id'] = str(uuid.uuid4())
        data['timestamp'] = data.get('timestamp', '')
        if data.get('is_private') and data.get('target'):
            target_name = data['target']
            target_sid = None
            for sid, name in active_rooms[room_code]['users'].items():
                if name == target_name:
                    target_sid = sid
                    break
            if target_sid:
                emit('receive_message', data, to=target_sid)
                emit('receive_message', data, to=request.sid)
            else:
                emit('receive_message', {'user': 'System', 'text': f'User {target_name} not found.'}, to=request.sid)
        else:
            emit('receive_message', data, to=room_code)

@socketio.on('delete_message')
def handle_delete(data):
    room_code = user_room_map.get(request.sid)
    if room_code:
        emit('message_deleted', data, to=room_code)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
