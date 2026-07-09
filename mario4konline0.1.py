#!/usr/bin/env python3
# pr files = off
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    SUPER MARIO RPG – WORLDWIDE v0.1                          ║
║                         AC Networking 1.x                                    ║
║══════════════════════════════════════════════════════════════════════════════║
║  Pygame UI – Super Mario RPG (1996) overworld style                          ║
║  ROM-Free – All SMRPG maps built-in (pr files = off)                         ║
║  P2P Multiplayer with DS Download Play style discovery                      ║
║  Features: Chat, player list, all RPG maps, party characters                ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

PR_FILES = "off"

import pygame
import socket
import threading
import json
import hashlib
import struct
import time
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Callable
from enum import Enum
import queue

# ==============================================================================
# AC NETWORKING 1.x (unchanged, except for callback integration)
# ==============================================================================

class PacketType(Enum):
    DISCOVERY = 0x01
    DISCOVERY_RESPONSE = 0x02
    JOIN_REQUEST = 0x03
    JOIN_ACCEPT = 0x04
    JOIN_DENY = 0x05
    PLAYER_UPDATE = 0x10
    PLAYER_POSITION = 0x11
    PLAYER_ACTION = 0x12
    CHAT_MESSAGE = 0x20
    ROOM_STATE = 0x30
    SYNC_REQUEST = 0x40
    SYNC_DATA = 0x41
    PING = 0xF0
    PONG = 0xF1
    DISCONNECT = 0xFF

@dataclass
class NetworkPlayer:
    player_id: str
    username: str
    address: Tuple[str, int]
    x: float = 400.0
    y: float = 300.0
    sprite_state: str = "idle"
    character: str = "mario"
    last_seen: float = field(default_factory=time.time)
    latency: int = 0

@dataclass
class ACPacket:
    magic: bytes = b'AC01'
    version: int = 1
    packet_type: PacketType = PacketType.PING
    player_id: str = ""
    payload: bytes = b""
    timestamp: float = field(default_factory=time.time)

    def serialize(self) -> bytes:
        payload_data = self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode()
        header = struct.pack(
            '<4sBB32sI',
            self.magic,
            self.version,
            self.packet_type.value,
            self.player_id.encode().ljust(32, b'\x00'),
            len(payload_data)
        )
        return header + payload_data

    @classmethod
    def deserialize(cls, data: bytes) -> 'ACPacket':
        if len(data) < 42:
            raise ValueError("Packet too small")
        magic, version, ptype, player_id_bytes, payload_len = struct.unpack(
            '<4sBB32sI', data[:42]
        )
        if magic != b'AC01':
            raise ValueError("Invalid packet magic")
        payload = data[42:42 + payload_len]
        return cls(
            magic=magic,
            version=version,
            packet_type=PacketType(ptype),
            player_id=player_id_bytes.rstrip(b'\x00').decode(),
            payload=payload
        )

class ACNetwork:
    DISCOVERY_PORT = 31337
    GAME_PORT = 31338
    BROADCAST_INTERVAL = 2.0
    TIMEOUT = 10.0

    def __init__(self, username: str):
        self.username = username
        self.player_id = hashlib.md5(f"{username}{time.time()}".encode()).hexdigest()[:16]
        self.players: Dict[str, NetworkPlayer] = {}
        self.is_host = False
        self.room_code = ""
        self.current_map = "marios_pad"
        self.running = False

        self.discovery_socket: Optional[socket.socket] = None
        self.game_socket: Optional[socket.socket] = None

        # Callbacks (will be set by the UI)
        self.on_player_join: Optional[Callable] = None
        self.on_player_leave: Optional[Callable] = None
        self.on_player_update: Optional[Callable] = None
        self.on_chat_message: Optional[Callable] = None
        self.on_room_discovered: Optional[Callable] = None
        self.on_room_state: Optional[Callable] = None

        self.threads: List[threading.Thread] = []
        self._lock = threading.Lock()

    def generate_room_code(self) -> str:
        chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return ''.join(random.choice(chars) for _ in range(6))

    def start_host(self) -> str:
        self.is_host = True
        self.room_code = self.generate_room_code()
        self.running = True

        self.discovery_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.discovery_socket.bind(('', self.DISCOVERY_PORT))

        self.game_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.game_socket.bind(('', self.GAME_PORT))

        t1 = threading.Thread(target=self._discovery_listener, daemon=True)
        t2 = threading.Thread(target=self._game_listener, daemon=True)
        t3 = threading.Thread(target=self._broadcast_room, daemon=True)
        self.threads = [t1, t2, t3]
        for t in self.threads:
            t.start()

        return self.room_code

    def start_client(self):
        self.is_host = False
        self.running = True

        self.discovery_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.discovery_socket.settimeout(1.0)

        self.game_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.game_socket.bind(('', 0))

        t1 = threading.Thread(target=self._client_discovery, daemon=True)
        t2 = threading.Thread(target=self._game_listener, daemon=True)
        self.threads = [t1, t2]
        for t in self.threads:
            t.start()

    def _broadcast_room(self):
        while self.running and self.is_host:
            try:
                packet = ACPacket(
                    packet_type=PacketType.DISCOVERY,
                    player_id=self.player_id,
                    payload=json.dumps({
                        "room_code": self.room_code,
                        "host_name": self.username,
                        "players": len(self.players) + 1,
                        "max_players": 8,
                        "game": "Super Mario RPG Worldwide v0.1"
                    }).encode()
                )
                self.discovery_socket.sendto(packet.serialize(), ('<broadcast>', self.DISCOVERY_PORT))
            except Exception:
                pass
            time.sleep(self.BROADCAST_INTERVAL)

    def _discovery_listener(self):
        while self.running and self.is_host:
            try:
                self.discovery_socket.settimeout(1.0)
                data, addr = self.discovery_socket.recvfrom(4096)
                packet = ACPacket.deserialize(data)
                if packet.packet_type == PacketType.JOIN_REQUEST:
                    self._handle_join_request(packet, addr)
            except socket.timeout:
                continue
            except Exception:
                pass

    def _client_discovery(self):
        search_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        search_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        search_socket.bind(('', self.DISCOVERY_PORT))
        search_socket.settimeout(1.0)
        while self.running and not self.is_host:
            try:
                data, addr = search_socket.recvfrom(4096)
                packet = ACPacket.deserialize(data)
                if packet.packet_type == PacketType.DISCOVERY:
                    room_info = json.loads(packet.payload.decode())
                    room_info['address'] = addr
                    if self.on_room_discovered:
                        self.on_room_discovered(room_info)
            except socket.timeout:
                continue
            except Exception:
                pass
        search_socket.close()

    def _game_listener(self):
        self.game_socket.settimeout(1.0)
        while self.running:
            try:
                data, addr = self.game_socket.recvfrom(4096)
                packet = ACPacket.deserialize(data)
                self._handle_game_packet(packet, addr)
            except socket.timeout:
                self._check_timeouts()
            except Exception:
                pass

    def _handle_join_request(self, packet: ACPacket, addr: Tuple[str, int]):
        try:
            join_data = json.loads(packet.payload.decode())
            player = NetworkPlayer(
                player_id=packet.player_id,
                username=join_data.get('username', 'Unknown'),
                address=addr,
                character=join_data.get('character', 'mario')
            )
            with self._lock:
                self.players[packet.player_id] = player
            response = ACPacket(
                packet_type=PacketType.JOIN_ACCEPT,
                player_id=self.player_id,
                payload=json.dumps({
                    "room_code": self.room_code,
                    "your_id": packet.player_id,
                    "players": [
                        {"id": p.player_id, "name": p.username, "char": p.character}
                        for p in self.players.values()
                    ],
                    "map_id": self.current_map
                }).encode()
            )
            self.game_socket.sendto(response.serialize(), addr)
            if self.on_player_join:
                self.on_player_join(player)
        except Exception:
            pass

    def _handle_game_packet(self, packet: ACPacket, addr: Tuple[str, int]):
        handlers = {
            PacketType.PLAYER_UPDATE: self._handle_player_update,
            PacketType.PLAYER_POSITION: self._handle_player_position,
            PacketType.CHAT_MESSAGE: self._handle_chat,
            PacketType.PING: self._handle_ping,
            PacketType.PONG: self._handle_pong,
            PacketType.DISCONNECT: self._handle_disconnect,
            PacketType.JOIN_ACCEPT: self._handle_join_accept,
            PacketType.ROOM_STATE: self._handle_room_state,
        }
        handler = handlers.get(packet.packet_type)
        if handler:
            handler(packet, addr)

    def _handle_player_update(self, packet: ACPacket, addr: Tuple[str, int]):
        try:
            data = json.loads(packet.payload.decode())
            with self._lock:
                if packet.player_id in self.players:
                    player = self.players[packet.player_id]
                    player.x = data.get('x', player.x)
                    player.y = data.get('y', player.y)
                    player.sprite_state = data.get('state', player.sprite_state)
                    player.last_seen = time.time()
                    if self.on_player_update:
                        self.on_player_update(player)
        except Exception:
            pass

    def _handle_player_position(self, packet: ACPacket, addr: Tuple[str, int]):
        try:
            x, y = struct.unpack('<ff', packet.payload[:8])
            with self._lock:
                if packet.player_id in self.players:
                    player = self.players[packet.player_id]
                    player.x = x
                    player.y = y
                    player.last_seen = time.time()
                    if self.on_player_update:
                        self.on_player_update(player)
        except Exception:
            pass

    def _handle_chat(self, packet: ACPacket, addr: Tuple[str, int]):
        try:
            message = packet.payload.decode('utf-8')
            with self._lock:
                username = "Unknown"
                if packet.player_id in self.players:
                    username = self.players[packet.player_id].username
            if self.on_chat_message:
                self.on_chat_message(username, message)
        except Exception:
            pass

    def _handle_ping(self, packet: ACPacket, addr: Tuple[str, int]):
        pong = ACPacket(
            packet_type=PacketType.PONG,
            player_id=self.player_id,
            payload=packet.payload
        )
        self.game_socket.sendto(pong.serialize(), addr)

    def _handle_pong(self, packet: ACPacket, addr: Tuple[str, int]):
        try:
            sent_time = struct.unpack('<d', packet.payload)[0]
            latency = int((time.time() - sent_time) * 1000)
            with self._lock:
                if packet.player_id in self.players:
                    self.players[packet.player_id].latency = latency
        except Exception:
            pass

    def _handle_disconnect(self, packet: ACPacket, addr: Tuple[str, int]):
        with self._lock:
            if packet.player_id in self.players:
                player = self.players.pop(packet.player_id)
                if self.on_player_leave:
                    self.on_player_leave(player)

    def _handle_join_accept(self, packet: ACPacket, addr: Tuple[str, int]):
        try:
            data = json.loads(packet.payload.decode())
            self.room_code = data.get('room_code', '')
            map_id = data.get('map_id', 'marios_pad')
            if self.on_room_state:
                self.on_room_state(map_id)
            for p_data in data.get('players', []):
                player = NetworkPlayer(
                    player_id=p_data['id'],
                    username=p_data['name'],
                    address=addr,
                    character=p_data.get('char', 'mario')
                )
                self.players[p_data['id']] = player
        except Exception:
            pass

    def _handle_room_state(self, packet: ACPacket, addr: Tuple[str, int]):
        try:
            data = json.loads(packet.payload.decode())
            map_id = data.get('map_id', 'marios_pad')
            self.current_map = map_id
            if self.on_room_state:
                self.on_room_state(map_id)
        except Exception:
            pass

    def send_map_change(self, map_id: str):
        self.current_map = map_id
        packet = ACPacket(
            packet_type=PacketType.ROOM_STATE,
            player_id=self.player_id,
            payload=json.dumps({"map_id": map_id}).encode()
        )
        self._broadcast_to_players(packet)

    def _check_timeouts(self):
        current_time = time.time()
        timed_out = []
        with self._lock:
            for pid, player in self.players.items():
                if current_time - player.last_seen > self.TIMEOUT:
                    timed_out.append(pid)
        for pid in timed_out:
            with self._lock:
                player = self.players.pop(pid, None)
            if player and self.on_player_leave:
                self.on_player_leave(player)

    def join_room(self, host_address: Tuple[str, int], room_code: str, character: str = "mario"):
        packet = ACPacket(
            packet_type=PacketType.JOIN_REQUEST,
            player_id=self.player_id,
            payload=json.dumps({
                "username": self.username,
                "room_code": room_code,
                "character": character
            }).encode()
        )
        self.discovery_socket.sendto(packet.serialize(), host_address)

    def send_position(self, x: float, y: float):
        packet = ACPacket(
            packet_type=PacketType.PLAYER_POSITION,
            player_id=self.player_id,
            payload=struct.pack('<ff', x, y)
        )
        self._broadcast_to_players(packet)

    def send_chat(self, message: str):
        packet = ACPacket(
            packet_type=PacketType.CHAT_MESSAGE,
            player_id=self.player_id,
            payload=message.encode('utf-8')
        )
        self._broadcast_to_players(packet)

    def _broadcast_to_players(self, packet: ACPacket):
        data = packet.serialize()
        with self._lock:
            for player in self.players.values():
                try:
                    self.game_socket.sendto(data, player.address)
                except Exception:
                    pass

    def disconnect(self):
        self.running = False
        packet = ACPacket(
            packet_type=PacketType.DISCONNECT,
            player_id=self.player_id
        )
        self._broadcast_to_players(packet)
        if self.discovery_socket:
            self.discovery_socket.close()
        if self.game_socket:
            self.game_socket.close()

# ==============================================================================
# SUPER MARIO RPG – Built-in maps (pr files = off)
# ==============================================================================

SCREEN_WIDTH = 800
SCREEN_HEIGHT = 600
MAP_WIDTH = 800
MAP_HEIGHT = 500
CHAT_HEIGHT = 100
PLAYER_LIST_WIDTH = 150

SMRPG_CHARACTERS = {
    "mario": (220, 40, 40),
    "mallow": (240, 240, 255),
    "geno": (60, 120, 220),
    "bowser": (180, 50, 30),
    "peach": (255, 170, 200),
    "toad": (255, 255, 255),
}

def _plat(x, y, w, h=20):
    return pygame.Rect(x, y, w, h)

def _build_smrpg_maps():
  """All Super Mario RPG overworld locations – procedural, no external files."""
  layouts = {
    "marios_pad": {
      "name": "Mario's Pad",
      "bg": (120, 190, 110), "ground": (70, 150, 65), "accent": (190, 140, 70),
      "platforms": [_plat(0, 460, 800), _plat(120, 380, 180), _plat(420, 340, 200), _plat(620, 400, 140)],
      "decorations": [("house", 80, 320), ("tree", 300, 300), ("tree", 550, 280), ("pipe", 700, 420)],
    },
    "mushroom_way": {
      "name": "Mushroom Way",
      "bg": (140, 200, 130), "ground": (90, 160, 80), "accent": (220, 180, 90),
      "platforms": [_plat(0, 470, 800), _plat(60, 390, 140), _plat(280, 350, 160), _plat(500, 310, 180), _plat(680, 380, 100)],
      "decorations": [("mushroom", 150, 350), ("sign", 400, 270), ("tree", 620, 260)],
    },
    "mushroom_kingdom": {
      "name": "Mushroom Kingdom",
      "bg": (150, 180, 220), "ground": (180, 160, 200), "accent": (255, 220, 100),
      "platforms": [_plat(0, 465, 800), _plat(100, 380, 220), _plat(380, 340, 200), _plat(580, 300, 180)],
      "decorations": [("castle", 200, 280), ("toadhouse", 450, 260), ("flag", 650, 240)],
    },
    "bandits_way": {
      "name": "Bandit's Way",
      "bg": (190, 170, 120), "ground": (150, 130, 80), "accent": (100, 80, 50),
      "platforms": [_plat(0, 475, 800), _plat(40, 400, 120), _plat(220, 360, 100), _plat(400, 320, 140), _plat(580, 370, 160)],
      "decorations": [("cactus", 100, 360), ("rock", 350, 280), ("chest", 620, 330)],
    },
    "kero_sewers": {
      "name": "Kero Sewers",
      "bg": (60, 80, 70), "ground": (40, 60, 50), "accent": (100, 140, 90),
      "platforms": [_plat(0, 480, 800), _plat(80, 400, 160), _plat(300, 360, 140), _plat(520, 400, 200)],
      "decorations": [("water", 200, 430), ("pipe", 450, 360), ("frog", 650, 370)],
    },
    "midas_river": {
      "name": "Midas River",
      "bg": (130, 200, 230), "ground": (80, 150, 200), "accent": (255, 215, 80),
      "platforms": [_plat(0, 470, 800), _plat(50, 390, 100), _plat(220, 350, 120), _plat(420, 310, 100), _plat(600, 350, 150)],
      "decorations": [("water", 0, 440), ("barrel", 180, 320), ("coin", 500, 270)],
    },
    "tadpole_pond": {
      "name": "Tadpole Pond",
      "bg": (100, 180, 220), "ground": (60, 140, 180), "accent": (120, 200, 120),
      "platforms": [_plat(0, 465, 800), _plat(140, 380, 200), _plat(400, 340, 180), _plat(620, 390, 150)],
      "decorations": [("pond", 250, 400), ("lily", 320, 410), ("frog", 500, 300), ("tadpole", 680, 350)],
    },
    "rose_way": {
      "name": "Rose Way",
      "bg": (180, 140, 160), "ground": (140, 100, 120), "accent": (255, 120, 160),
      "platforms": [_plat(0, 470, 800), _plat(70, 390, 150), _plat(280, 350, 130), _plat(480, 310, 160), _plat(660, 370, 120)],
      "decorations": [("flower", 120, 350), ("flower", 350, 280), ("bush", 580, 260)],
    },
    "forest_maze": {
      "name": "Forest Maze",
      "bg": (50, 100, 60), "ground": (30, 70, 40), "accent": (80, 140, 70),
      "platforms": [_plat(0, 475, 800), _plat(60, 400, 100), _plat(200, 360, 90), _plat(350, 320, 110), _plat(500, 370, 100), _plat(650, 330, 120)],
      "decorations": [("tree", 100, 360), ("tree", 250, 300), ("tree", 420, 280), ("tree", 600, 290), ("maze", 380, 200)],
    },
    "rose_town": {
      "name": "Rose Town",
      "bg": (200, 150, 170), "ground": (160, 110, 130), "accent": (255, 100, 140),
      "platforms": [_plat(0, 465, 800), _plat(100, 380, 180), _plat(350, 340, 200), _plat(580, 300, 180)],
      "decorations": [("inn", 180, 300), ("flower", 400, 290), ("music", 620, 260)],
    },
    "yoster_isle": {
      "name": "Yo'ster Isle",
      "bg": (160, 210, 140), "ground": (120, 170, 100), "accent": (255, 200, 60),
      "platforms": [_plat(0, 470, 800), _plat(80, 390, 160), _plat(300, 350, 180), _plat(520, 310, 200), _plat(700, 380, 90)],
      "decorations": [("yoshi", 200, 330), ("egg", 450, 270), ("palm", 650, 260)],
    },
    "moleville": {
      "name": "Moleville",
      "bg": (140, 120, 100), "ground": (100, 80, 60), "accent": (180, 150, 100),
      "platforms": [_plat(0, 475, 800), _plat(50, 400, 140), _plat(250, 360, 160), _plat(450, 320, 140), _plat(630, 380, 150)],
      "decorations": [("mine", 150, 340), ("cart", 380, 280), ("mole", 600, 330)],
    },
    "booster_hill": {
      "name": "Booster Hill",
      "bg": (210, 180, 150), "ground": (170, 140, 110), "accent": (255, 80, 80),
      "platforms": [_plat(0, 480, 800), _plat(100, 420, 120), _plat(250, 370, 110), _plat(400, 320, 100), _plat(550, 270, 110), _plat(700, 220, 100)],
      "decorations": [("train", 180, 380), ("tower", 480, 230), ("flag", 720, 180)],
    },
    "marrymore": {
      "name": "Marrymore",
      "bg": (220, 200, 230), "ground": (180, 160, 190), "accent": (255, 220, 180),
      "platforms": [_plat(0, 465, 800), _plat(120, 380, 200), _plat(380, 340, 220), _plat(600, 300, 180)],
      "decorations": [("church", 220, 280), ("bell", 450, 260), ("rose", 650, 250)],
    },
    "bean_valley": {
      "name": "Bean Valley",
      "bg": (100, 180, 100), "ground": (60, 140, 60), "accent": (140, 200, 80),
      "platforms": [_plat(0, 470, 800), _plat(60, 390, 130), _plat(230, 350, 120), _plat(400, 310, 130), _plat(570, 350, 140), _plat(720, 300, 80)],
      "decorations": [("beanstalk", 150, 320), ("vine", 350, 260), ("cloud", 600, 220)],
    },
    "lands_end": {
      "name": "Land's End",
      "bg": (180, 200, 160), "ground": (140, 160, 120), "accent": (200, 180, 100),
      "platforms": [_plat(0, 475, 800), _plat(80, 400, 150), _plat(300, 360, 140), _plat(500, 320, 160), _plat(680, 380, 110)],
      "decorations": [("cliff", 200, 340), ("whale", 450, 280), ("star", 650, 300)],
    },
    "nimbus_land": {
      "name": "Nimbus Land",
      "bg": (200, 160, 230), "ground": (170, 130, 200), "accent": (255, 220, 100),
      "platforms": [_plat(0, 460, 800), _plat(100, 380, 180), _plat(350, 340, 200), _plat(580, 300, 180), _plat(700, 380, 90)],
      "decorations": [("cloud", 150, 300), ("palace", 400, 260), ("valentina", 620, 240)],
    },
    "barrel_volcano": {
      "name": "Barrel Volcano",
      "bg": (80, 30, 20), "ground": (120, 50, 30), "accent": (255, 120, 40),
      "platforms": [_plat(0, 480, 800), _plat(80, 410, 120), _plat(240, 370, 110), _plat(400, 330, 100), _plat(560, 290, 110), _plat(700, 350, 90)],
      "decorations": [("lava", 200, 440), ("lava", 500, 430), ("volcano", 380, 200)],
    },
    "sunken_ship": {
      "name": "Sunken Ship",
      "bg": (40, 60, 100), "ground": (60, 80, 120), "accent": (140, 160, 180),
      "platforms": [_plat(0, 470, 800), _plat(100, 390, 180), _plat(350, 350, 200), _plat(580, 310, 180)],
      "decorations": [("ship", 200, 320), ("anchor", 450, 280), ("ghost", 650, 260)],
    },
    "monstro_town": {
      "name": "Monstro Town",
      "bg": (100, 80, 140), "ground": (80, 60, 110), "accent": (180, 140, 220),
      "platforms": [_plat(0, 465, 800), _plat(120, 380, 200), _plat(380, 340, 180), _plat(600, 300, 170)],
      "decorations": [("monster", 200, 320), ("door", 420, 280), ("culex", 640, 240)],
    },
    "bowser_keep": {
      "name": "Bowser's Keep",
      "bg": (60, 40, 50), "ground": (90, 60, 70), "accent": (200, 60, 40),
      "platforms": [_plat(0, 475, 800), _plat(80, 400, 160), _plat(300, 360, 180), _plat(520, 320, 200), _plat(680, 380, 110)],
      "decorations": [("lava", 150, 440), ("bridge", 400, 290), ("throne", 620, 260)],
    },
    "smithys_factory": {
      "name": "Smithy's Factory",
      "bg": (70, 70, 80), "ground": (90, 90, 100), "accent": (160, 160, 180),
      "platforms": [_plat(0, 480, 800), _plat(60, 410, 140), _plat(240, 370, 130), _plat(420, 330, 140), _plat(600, 290, 150), _plat(720, 360, 70)],
      "decorations": [("gear", 150, 360), ("conveyor", 400, 290), ("smithy", 620, 230)],
    },
    "star_hill": {
      "name": "Star Hill",
      "bg": (30, 20, 60), "ground": (50, 40, 90), "accent": (255, 255, 150),
      "platforms": [_plat(0, 465, 800), _plat(150, 380, 180), _plat(380, 340, 200), _plat(580, 300, 180)],
      "decorations": [("star", 200, 300), ("star", 350, 280), ("star", 500, 260), ("wish", 650, 240)],
    },
    "belome_temple": {
      "name": "Belome Temple",
      "bg": (90, 70, 50), "ground": (110, 90, 70), "accent": (200, 170, 100),
      "platforms": [_plat(0, 475, 800), _plat(100, 400, 150), _plat(300, 360, 140), _plat(500, 320, 160), _plat(680, 380, 110)],
      "decorations": [("pillar", 180, 350), ("dog", 420, 280), ("treasure", 620, 330)],
    },
    "seaside_town": {
      "name": "Seaside Town",
      "bg": (140, 200, 230), "ground": (200, 180, 140), "accent": (80, 160, 220),
      "platforms": [_plat(0, 470, 800), _plat(100, 390, 180), _plat(350, 350, 200), _plat(580, 310, 180)],
      "decorations": [("pier", 200, 340), ("boat", 450, 280), ("shell", 650, 270)],
    },
  }
  for key, data in layouts.items():
    data["platforms"] = [pygame.Rect(*p) if not isinstance(p, pygame.Rect) else p for p in data["platforms"]]
  return layouts

SMRPG_MAPS: Dict[str, dict] = {}
SMRPG_MAP_ORDER: List[str] = []

COLOR_TEXT = (255, 255, 255)
COLOR_CHAT_BG = (0, 0, 0, 180)
COLOR_PLAYER_LIST_BG = (50, 50, 50, 200)

def _draw_smrpg_decoration(screen, deco_type: str, x: int, y: int, accent):
  if deco_type == "tree":
    pygame.draw.rect(screen, (100, 60, 30), (x, y + 20, 12, 30))
    pygame.draw.circle(screen, (40, 120, 40), (x + 6, y), 22)
  elif deco_type == "house":
    pygame.draw.rect(screen, (200, 160, 100), (x, y, 60, 50))
    pygame.draw.polygon(screen, (180, 60, 40), [(x - 5, y), (x + 30, y - 25), (x + 65, y)])
  elif deco_type == "castle":
    pygame.draw.rect(screen, (180, 180, 200), (x, y, 80, 70))
    pygame.draw.rect(screen, accent, (x + 30, y + 30, 20, 40))
  elif deco_type == "lava":
    pygame.draw.ellipse(screen, (255, 100, 20), (x, y, 80, 30))
  elif deco_type == "water" or deco_type == "pond":
    pygame.draw.ellipse(screen, (60, 120, 220), (x, y, 100, 40))
  elif deco_type == "cloud":
    pygame.draw.ellipse(screen, (255, 255, 255), (x, y, 70, 30))
    pygame.draw.ellipse(screen, (255, 255, 255), (x + 30, y - 10, 50, 25))
  elif deco_type == "star":
    pygame.draw.circle(screen, accent, (x + 8, y + 8), 10)
  elif deco_type == "flower":
    pygame.draw.circle(screen, accent, (x, y, 16))
    pygame.draw.circle(screen, (255, 255, 100), (x + 8, y + 8), 6)
  elif deco_type == "mushroom":
    pygame.draw.rect(screen, (240, 220, 200), (x + 6, y + 12, 10, 16))
    pygame.draw.ellipse(screen, (220, 40, 40), (x, y, 22, 16))
  elif deco_type == "pipe":
    pygame.draw.rect(screen, (40, 180, 60), (x, y, 30, 50))
    pygame.draw.rect(screen, (60, 220, 80), (x - 4, y, 38, 14))
  elif deco_type == "volcano":
    pygame.draw.polygon(screen, (80, 30, 20), [(x, y + 80), (x + 60, y), (x + 120, y + 80)])
    pygame.draw.ellipse(screen, (255, 120, 30), (x + 40, y - 5, 40, 20))
  elif deco_type == "ship":
    pygame.draw.polygon(screen, (120, 100, 80), [(x, y + 40), (x + 80, y + 40), (x + 60, y), (x + 20, y)])
    pygame.draw.rect(screen, (200, 200, 220), (x + 30, y - 30, 20, 35))
  elif deco_type == "gear":
    pygame.draw.circle(screen, accent, (x + 15, y + 15), 18)
    pygame.draw.circle(screen, (70, 70, 80), (x + 15, y + 15), 8)
  else:
    pygame.draw.rect(screen, accent, (x, y, 24, 24), border_radius=4)

class GameClient:
    def __init__(self):
        pygame.init()
        global SMRPG_MAPS, SMRPG_MAP_ORDER
        if not SMRPG_MAPS:
            SMRPG_MAPS.update(_build_smrpg_maps())
            SMRPG_MAP_ORDER.extend(SMRPG_MAPS.keys())

        self.screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
        pygame.display.set_caption("Super Mario RPG Worldwide v0.1 – AC Networking 1.x")
        self.clock = pygame.time.Clock()
        self.font = pygame.font.Font(None, 24)
        self.small_font = pygame.font.Font(None, 18)

        # Network
        self.network: Optional[ACNetwork] = None
        self.username = f"Player{random.randint(1000,9999)}"
        self.room_code = ""

        # Game objects
        self.local_player: Optional[NetworkPlayer] = None
        self.players: Dict[str, NetworkPlayer] = {}
        self.current_map_id = "marios_pad"
        self.map_index = 0
        self.character_index = 0
        self.character_keys = list(SMRPG_CHARACTERS.keys())

        # Chat
        self.chat_messages: List[Tuple[str, str]] = []
        self.chat_input = ""
        self.input_active = False

        # Discovered rooms
        self.discovered_rooms: List[dict] = []
        self.selected_room = 0

        # UI state
        self.state = "menu"
        self.error_message = ""

        self.event_queue = queue.Queue()

        # Menu elements
        self.username_input = self.username
        self.menu_options = ["Host Game", "Search for Games", "Quit"]
        self.selected_option = 0

        self.run()

    # --------------------------------------------------------------------------
    # Network callbacks (called from network threads, enqueue events)
    # --------------------------------------------------------------------------
    def on_room_discovered(self, room_info: dict):
        self.event_queue.put(("room_discovered", room_info))

    def on_player_join(self, player: NetworkPlayer):
        self.event_queue.put(("player_join", player))

    def on_player_leave(self, player: NetworkPlayer):
        self.event_queue.put(("player_leave", player))

    def on_player_update(self, player: NetworkPlayer):
        self.event_queue.put(("player_update", player))

    def on_chat_message(self, username: str, message: str):
        self.event_queue.put(("chat", username, message))

    def on_room_state(self, map_id: str):
        self.event_queue.put(("map_change", map_id))

    def get_current_map(self) -> dict:
        return SMRPG_MAPS.get(self.current_map_id, SMRPG_MAPS["marios_pad"])

    def set_map(self, map_id: str, broadcast: bool = False):
        if map_id not in SMRPG_MAPS:
            return
        if map_id == self.current_map_id and not broadcast:
            return
        self.current_map_id = map_id
        self.map_index = SMRPG_MAP_ORDER.index(map_id)
        spawn = self.get_spawn_point()
        if self.local_player:
            self.local_player.x, self.local_player.y = spawn
            if self.network:
                self.players[self.network.player_id] = self.local_player
                self.network.send_position(spawn[0], spawn[1])
        if broadcast and self.network and self.network.is_host:
            self.network.send_map_change(map_id)
        self.add_chat_message("System", f"Map: {SMRPG_MAPS[map_id]['name']}")

    def get_spawn_point(self) -> Tuple[float, float]:
        plats = self.get_current_map()["platforms"]
        if plats:
            p = plats[0]
            return (p.centerx, p.top - 30)
        return (400.0, 300.0)

    # --------------------------------------------------------------------------
    # Event processing
    # --------------------------------------------------------------------------
    def process_events(self):
        try:
            while True:
                event = self.event_queue.get_nowait()
                if event[0] == "room_discovered":
                    room_info = event[1]
                    # Avoid duplicates
                    if not any(r['room_code'] == room_info['room_code'] for r in self.discovered_rooms):
                        self.discovered_rooms.append(room_info)
                elif event[0] == "player_join":
                    player = event[1]
                    self.players[player.player_id] = player
                    self.add_chat_message("System", f"{player.username} joined")
                elif event[0] == "player_leave":
                    player = event[1]
                    if player.player_id in self.players:
                        del self.players[player.player_id]
                    self.add_chat_message("System", f"{player.username} left")
                elif event[0] == "player_update":
                    player = event[1]
                    if player.player_id in self.players:
                        self.players[player.player_id].x = player.x
                        self.players[player.player_id].y = player.y
                elif event[0] == "chat":
                    username, msg = event[1], event[2]
                    self.add_chat_message(username, msg)
                elif event[0] == "map_change":
                    self.set_map(event[1], broadcast=False)
        except queue.Empty:
            pass

    def add_chat_message(self, sender: str, message: str):
        self.chat_messages.append((sender, message))
        if len(self.chat_messages) > 20:
            self.chat_messages.pop(0)

    # --------------------------------------------------------------------------
    # Drawing
    # --------------------------------------------------------------------------
    def draw_menu(self):
        self.screen.fill((20, 10, 40))

        title = self.font.render("SUPER MARIO RPG WORLDWIDE", True, (255, 220, 80))
        self.screen.blit(title, (SCREEN_WIDTH//2 - title.get_width()//2, 80))

        sub = self.small_font.render("pr files = off  |  All maps built-in", True, (180, 180, 200))
        self.screen.blit(sub, (SCREEN_WIDTH//2 - sub.get_width()//2, 115))

        char_name = self.character_keys[self.character_index]
        char_text = self.small_font.render(
            f"Character: {char_name.title()}  (< > to change)", True, SMRPG_CHARACTERS[char_name]
        )
        self.screen.blit(char_text, (SCREEN_WIDTH//2 - 140, 160))

        user_text = self.small_font.render(f"Username: {self.username_input}", True, COLOR_TEXT)
        self.screen.blit(user_text, (SCREEN_WIDTH//2 - 100, 200))

        # Options
        for i, opt in enumerate(self.menu_options):
            color = (255, 255, 0) if i == self.selected_option else COLOR_TEXT
            text = self.font.render(opt, True, color)
            self.screen.blit(text, (SCREEN_WIDTH//2 - text.get_width()//2, 300 + i*40))

        if self.error_message:
            err = self.small_font.render(self.error_message, True, (255, 0, 0))
            self.screen.blit(err, (SCREEN_WIDTH//2 - err.get_width()//2, 500))

    def draw_game(self):
        mdata = self.get_current_map()
        self.screen.fill(mdata["bg"])

        for deco in mdata.get("decorations", []):
            _draw_smrpg_decoration(self.screen, deco[0], deco[1], deco[2], mdata["accent"])

        for plat in mdata["platforms"]:
            pygame.draw.rect(self.screen, mdata["ground"], plat)
            pygame.draw.rect(self.screen, mdata["accent"], plat, 2)
            highlight = tuple(min(255, c + 30) for c in mdata["ground"])
            pygame.draw.line(self.screen, highlight, (plat.left, plat.top), (plat.right, plat.top), 2)

        for pid, player in self.players.items():
            color = SMRPG_CHARACTERS.get(player.character, (220, 40, 40))
            rect = pygame.Rect(int(player.x) - 12, int(player.y) - 20, 24, 30)
            pygame.draw.rect(self.screen, color, rect, border_radius=4)
            pygame.draw.rect(self.screen, (0, 0, 0), rect, 2)
            eye_color = (255, 255, 255) if pid == self.network.player_id else (0, 0, 0)
            pygame.draw.circle(self.screen, eye_color, (int(player.x) - 4, int(player.y) - 12), 3)
            pygame.draw.circle(self.screen, eye_color, (int(player.x) + 4, int(player.y) - 12), 3)
            name_surf = self.small_font.render(player.username, True, COLOR_TEXT)
            self.screen.blit(name_surf, (int(player.x) - name_surf.get_width()//2, int(player.y) - 40))

        # Draw player list (right side)
        list_surf = pygame.Surface((PLAYER_LIST_WIDTH, SCREEN_HEIGHT), pygame.SRCALPHA)
        list_surf.fill(COLOR_PLAYER_LIST_BG)
        self.screen.blit(list_surf, (SCREEN_WIDTH - PLAYER_LIST_WIDTH, 0))

        y_offset = 10
        title = self.small_font.render("Players", True, COLOR_TEXT)
        self.screen.blit(title, (SCREEN_WIDTH - PLAYER_LIST_WIDTH + 10, y_offset))
        y_offset += 25
        for pid, player in self.players.items():
            text = f"{'👑' if pid == self.network.player_id else '👤'} {player.username}"
            if player.latency > 0:
                text += f" {player.latency}ms"
            surf = self.small_font.render(text, True, COLOR_TEXT)
            self.screen.blit(surf, (SCREEN_WIDTH - PLAYER_LIST_WIDTH + 10, y_offset))
            y_offset += 20

        # Draw chat area (bottom)
        chat_surf = pygame.Surface((SCREEN_WIDTH - PLAYER_LIST_WIDTH, CHAT_HEIGHT), pygame.SRCALPHA)
        chat_surf.fill(COLOR_CHAT_BG)
        self.screen.blit(chat_surf, (0, SCREEN_HEIGHT - CHAT_HEIGHT))

        y_offset = SCREEN_HEIGHT - CHAT_HEIGHT + 5
        for sender, msg in self.chat_messages[-5:]:
            text = f"{sender}: {msg}"
            surf = self.small_font.render(text, True, COLOR_TEXT)
            self.screen.blit(surf, (10, y_offset))
            y_offset += 20

        # Chat input line
        if self.input_active:
            input_text = f"> {self.chat_input}"
            surf = self.small_font.render(input_text, True, COLOR_TEXT)
            self.screen.blit(surf, (10, SCREEN_HEIGHT - 25))

        # Room code
        if self.room_code:
            code_text = self.small_font.render(f"Room: {self.room_code}", True, (255, 255, 0))
            self.screen.blit(code_text, (10, 10))

        map_text = self.small_font.render(f"{mdata['name']} ({self.map_index + 1}/{len(SMRPG_MAP_ORDER)})", True, (255, 220, 120))
        self.screen.blit(map_text, (10, 30))
        if self.state == "hosting":
            hint = self.small_font.render("[ ] change map", True, (180, 180, 180))
            self.screen.blit(hint, (10, 50))

    def draw_searching(self):
        self.screen.fill((20, 10, 40))
        text = self.font.render("Searching for RPG rooms...", True, COLOR_TEXT)
        self.screen.blit(text, (SCREEN_WIDTH//2 - text.get_width()//2, 180))

        y = 230
        for i, room in enumerate(self.discovered_rooms):
            prefix = "> " if i == self.selected_room else "  "
            room_str = f"{prefix}{room['room_code']} - {room['host_name']} ({room['players']}/{room['max_players']})"
            color = (255, 220, 80) if i == self.selected_room else COLOR_TEXT
            surf = self.small_font.render(room_str, True, color)
            self.screen.blit(surf, (SCREEN_WIDTH//2 - 180, y))
            y += 25

        if not self.discovered_rooms:
            wait = self.small_font.render("No rooms found yet...", True, COLOR_TEXT)
            self.screen.blit(wait, (SCREEN_WIDTH//2 - wait.get_width()//2, 250))

        back = self.small_font.render("ENTER join | UP/DOWN select | ESC menu", True, COLOR_TEXT)
        self.screen.blit(back, (10, SCREEN_HEIGHT - 30))

    # --------------------------------------------------------------------------
    # Main loop
    # --------------------------------------------------------------------------
    def run(self):
        running = True
        while running:
            # Process pygame events
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    self.handle_keydown(event)
                elif event.type == pygame.KEYUP:
                    self.handle_keyup(event)

            # Process network events from queue
            self.process_events()

            # Update game state (movement, etc.)
            self.update()

            # Draw everything
            if self.state == "menu":
                self.draw_menu()
            elif self.state == "game":
                self.draw_game()
            elif self.state == "searching":
                self.draw_searching()
            elif self.state == "hosting":
                self.draw_game()  # same as game, but hosting

            pygame.display.flip()
            self.clock.tick(60)

        self.cleanup()

    def handle_keydown(self, event):
        if self.state == "menu":
            if event.key == pygame.K_UP:
                self.selected_option = (self.selected_option - 1) % len(self.menu_options)
            elif event.key == pygame.K_DOWN:
                self.selected_option = (self.selected_option + 1) % len(self.menu_options)
            elif event.key == pygame.K_LEFT:
                self.character_index = (self.character_index - 1) % len(self.character_keys)
            elif event.key == pygame.K_RIGHT:
                self.character_index = (self.character_index + 1) % len(self.character_keys)
            elif event.key == pygame.K_RETURN:
                self.select_menu_option()
            elif event.key == pygame.K_BACKSPACE:
                self.username_input = self.username_input[:-1]
            else:
                if event.unicode.isprintable() and len(self.username_input) < 20:
                    self.username_input += event.unicode

        elif self.state in ("game", "hosting"):
            if event.key == pygame.K_RETURN:
                self.input_active = not self.input_active
                if not self.input_active and self.chat_input:
                    if self.network:
                        self.network.send_chat(self.chat_input)
                    self.add_chat_message(self.username, self.chat_input)
                    self.chat_input = ""
            elif self.input_active:
                if event.key == pygame.K_BACKSPACE:
                    self.chat_input = self.chat_input[:-1]
                elif event.unicode.isprintable() and len(self.chat_input) < 80:
                    self.chat_input += event.unicode
            elif self.state == "hosting":
                if event.key == pygame.K_LEFTBRACKET:
                    self.map_index = (self.map_index - 1) % len(SMRPG_MAP_ORDER)
                    self.set_map(SMRPG_MAP_ORDER[self.map_index], broadcast=True)
                elif event.key == pygame.K_RIGHTBRACKET:
                    self.map_index = (self.map_index + 1) % len(SMRPG_MAP_ORDER)
                    self.set_map(SMRPG_MAP_ORDER[self.map_index], broadcast=True)

        elif self.state == "searching":
            if event.key == pygame.K_ESCAPE:
                self.back_to_menu()
            elif event.key == pygame.K_UP and self.discovered_rooms:
                self.selected_room = (self.selected_room - 1) % len(self.discovered_rooms)
            elif event.key == pygame.K_DOWN and self.discovered_rooms:
                self.selected_room = (self.selected_room + 1) % len(self.discovered_rooms)
            elif event.key == pygame.K_RETURN and self.discovered_rooms:
                self.join_room(self.discovered_rooms[self.selected_room])

    def handle_keyup(self, event):
        pass

    def update(self):
        if self.state in ("game", "hosting") and self.local_player and self.network:
            keys = pygame.key.get_pressed()
            if self.input_active:
                return
            dx = dy = 0
            if keys[pygame.K_LEFT] or keys[pygame.K_a]:
                dx = -4
            if keys[pygame.K_RIGHT] or keys[pygame.K_d]:
                dx = 4
            if keys[pygame.K_UP] or keys[pygame.K_w]:
                dy = -4
            if keys[pygame.K_DOWN] or keys[pygame.K_s]:
                dy = 4

            if dx != 0 or dy != 0:
                new_x = max(20, min(MAP_WIDTH - PLAYER_LIST_WIDTH - 20, self.local_player.x + dx))
                new_y = max(40, min(MAP_HEIGHT - 40, self.local_player.y + dy))
                self.local_player.x = new_x
                self.local_player.y = new_y
                self.players[self.network.player_id] = self.local_player
                self.network.send_position(new_x, new_y)

    def select_menu_option(self):
        self.username = self.username_input.strip() or f"Player{random.randint(1000,9999)}"
        if self.menu_options[self.selected_option] == "Host Game":
            self.start_hosting()
        elif self.menu_options[self.selected_option] == "Search for Games":
            self.start_searching()
        elif self.menu_options[self.selected_option] == "Quit":
            pygame.quit()
            exit()

    def start_hosting(self):
        self.network = ACNetwork(self.username)
        self.network.on_room_discovered = self.on_room_discovered
        self.network.on_player_join = self.on_player_join
        self.network.on_player_leave = self.on_player_leave
        self.network.on_player_update = self.on_player_update
        self.network.on_chat_message = self.on_chat_message
        self.network.on_room_state = self.on_room_state

        try:
            char = self.character_keys[self.character_index]
            self.network.current_map = self.current_map_id
            self.room_code = self.network.start_host()
            spawn = self.get_spawn_point()
            self.local_player = NetworkPlayer(
                player_id=self.network.player_id,
                username=self.username,
                address=('', 0),
                x=spawn[0], y=spawn[1],
                character=char
            )
            self.players[self.network.player_id] = self.local_player
            self.state = "hosting"
            self.add_chat_message("System", f"Room created! Code: {self.room_code}")
            self.add_chat_message("System", f"Map: {self.get_current_map()['name']} | [ ] to change")
        except Exception as e:
            self.error_message = str(e)

    def start_searching(self):
        self.network = ACNetwork(self.username)
        self.network.on_room_discovered = self.on_room_discovered
        self.network.on_player_join = self.on_player_join
        self.network.on_player_leave = self.on_player_leave
        self.network.on_player_update = self.on_player_update
        self.network.on_chat_message = self.on_chat_message
        self.network.on_room_state = self.on_room_state

        try:
            self.network.start_client()
            self.state = "searching"
            self.discovered_rooms.clear()
            self.selected_room = 0
        except Exception as e:
            self.error_message = str(e)

    def join_room(self, room_info):
        if not self.network:
            return
        char = self.character_keys[self.character_index]
        self.network.join_room(room_info['address'], room_info['room_code'], char)
        spawn = self.get_spawn_point()
        self.local_player = NetworkPlayer(
            player_id=self.network.player_id,
            username=self.username,
            address=room_info['address'],
            x=spawn[0], y=spawn[1],
            character=char
        )
        self.players[self.network.player_id] = self.local_player
        self.room_code = room_info['room_code']
        self.state = "game"
        self.add_chat_message("System", f"Joined room {self.room_code}")

    def back_to_menu(self):
        if self.network:
            self.network.disconnect()
            self.network = None
        self.players.clear()
        self.local_player = None
        self.state = "menu"
        self.discovered_rooms.clear()
        self.chat_messages.clear()

    def cleanup(self):
        if self.network:
            self.network.disconnect()
        pygame.quit()

# ==============================================================================
# MAIN
# ==============================================================================
if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    SUPER MARIO RPG WORLDWIDE v0.1                              ║
║                         AC Networking 1.x                                    ║
║══════════════════════════════════════════════════════════════════════════════║
║  pr files = off – All 25 SMRPG maps built-in, no external assets             ║
║  Party: Mario, Mallow, Geno, Bowser, Peach, Toad                             ║
║  Controls: WASD/Arrows move | Enter chat | [ ] host changes map             ║
╚══════════════════════════════════════════════════════════════════════════════╝
    """)
    GameClient()
