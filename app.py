import threading
import time
import os
import re
import subprocess
import json
import traceback
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, jsonify, Response
from werkzeug.utils import secure_filename
from urllib.parse import unquote, urlparse, parse_qs
from datetime import datetime, timedelta
from threading import Lock
import logging
import ctypes
from ctypes.util import find_library
import shutil

# Configure logging
logging.basicConfig(level=logging.DEBUG)

# Mock Flask app and its config for demonstration purposes
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "supersecretkey")
app.config['DOWNLOAD_FOLDER'] = 'downloads'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB limit
app.config['STREAM_CHUNK_SIZE'] = 8 * 1024 * 1024  # 8MB chunks for streaming
os.makedirs(app.config['DOWNLOAD_FOLDER'], exist_ok=True)

# Create a lock for thread-safe header updates
yt_header_lock = Lock()

# Token refresh function (add this new function)
def refresh_youtube_token():
    try:
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Connection': 'keep-alive',
            'X-OAuth-Client-ID': '755541669657-kbosfavg7pk7sr3849c3tf657hpi5jpd.apps.googleusercontent.com',
            'Accept': '*/*',
            'User-Agent': 'com.google.ios.youtube/20.28.2 iSL/3.4 iPad/18.5 hw/iPad11_1 (gzip)',
            'Authorization': 'Bearer 1//0gq-BC5mJ2Kd6CgYIARAAGBASNwF-L9IrlyO1XnUNIvDJmnOYGm4oqpcdfC55X-xA7dSWDwmp-b2ley1sHw9FEz4YtJ_l97xzGgo',
            'Accept-Language': 'en-IN,en;q=0.9',
        }

        data = {
            'app_id': 'com.google.ios.youtube',
            'client_id': '755541669657-kbosfavg7pk7sr3849c3tf657hpi5jpd.apps.googleusercontent.com',
            'device_id': '516761AA-4836-4859-9AA9-DD350062C57C',
            'hl': 'en-IN',
            'lib_ver': '3.4',
            'response_type': 'token',
            'scope': 'https://www.googleapis.com/auth/youtube https://www.googleapis.com/auth/youtube.force-ssl https://www.googleapis.com/auth/identity.lateimpersonation https://www.googleapis.com/auth/supportcontent https://www.googleapis.com/auth/account_settings_mobile https://www.googleapis.com/auth/accounts.reauth',
        }

        response = requests.post(
            'https://oauthaccountmanager.googleapis.com/v1/issuetoken',
            headers=headers,
            data=data
        )
        
        if response.status_code == 200:
            token_data = response.json()
            new_token = token_data.get('token')
            if new_token:
                with yt_header_lock:
                    # Update the Authorization header
                    YT_HEADERS['Authorization'] = f'Bearer {new_token}'
                logging.error(f"Successfully refreshed YouTube token at {datetime.now()},token: {new_token}")
                return True
        logging.error(f"Token refresh failed: {response.status_code} - {response.text}")
    except Exception as e:
        logging.error(f"Error refreshing token: {str(e)}")
    return False

# Token refresh scheduler (add this new function)
def token_refresh_scheduler():
    while True:
        refresh_youtube_token()
        # Sleep for 50 minutes (3000 seconds)
        time.sleep(3000)

# Validate time format (HH:MM:SS or MM:SS or SS)
def validate_time(time_str):
    if not time_str:
        return True
    return re.match(r'^(\d{1,2}:)?([0-5]?\d:)?[0-5]?\d$', time_str)

# Convert time string to seconds
def time_to_seconds(time_str):
    if not time_str:
        return 0
    parts = list(map(int, time_str.split(':')))
    parts.reverse()  # Now parts are [seconds, minutes, hours] if they exist
    multipliers = [1, 60, 3600]
    seconds = 0
    for i in range(len(parts)):
        seconds += parts[i] * multipliers[i]
    return seconds

class ProtoFuck:
    def __init__(self, lib_path=None):
        if lib_path is None:
            lib_path = os.path.abspath("libprotoscope.so")
        self.lib = ctypes.CDLL(lib_path)
        
        # Disassemble: bytes -> str
        self.lib.Disassemble.argtypes = [
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int
        ]
        self.lib.Disassemble.restype = ctypes.c_char_p
        
        # Assemble: str -> bytes
        self.lib.Assemble.argtypes = [
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_int)
        ]
        self.lib.Assemble.restype = ctypes.POINTER(ctypes.c_ubyte)
        self.libc = ctypes.CDLL(find_library("c"))
        self.libc.free.argtypes = [ctypes.c_void_p]

    def disassemble(self, data: bytes) -> str:
        c_data = (ctypes.c_ubyte * len(data))(*data)
        result = self.lib.Disassemble(c_data, len(data))
        return result.decode('utf-8')

    def assemble(self, text: str) -> bytes:
        c_text = ctypes.c_char_p(text.encode('utf-8'))
        out_len = ctypes.c_int(0)
        
        c_data = self.lib.Assemble(c_text, ctypes.byref(out_len))
        if not c_data:
            raise ValueError("Protobuf assembly failed")
        try:
            result = bytes(ctypes.cast(
                c_data, 
                ctypes.POINTER(ctypes.c_ubyte * out_len.value)
            ).contents)
        finally:
            self.libc.free(c_data)
        return result
        
def cleanup_old_files():
    """Delete files older than 5 minutes from download folder"""
    now = time.time()
    folder = app.config['DOWNLOAD_FOLDER']
    for filename in os.listdir(folder):
        file_path = os.path.join(folder, filename)
        if os.path.isfile(file_path):
            mtime = os.path.getmtime(file_path)
            if now - mtime > 300:  # 5 minutes in seconds
                try:
                    os.remove(file_path)
                    logging.info(f"Cleaned up old file: {filename}")
                except Exception as e:
                    logging.error(f"Error cleaning up file {filename}: {str(e)}")

# Track download status
download_status = {}
download_lock = threading.Lock()

# Modify the YT_HEADERS initialization to use the lock
YT_HEADERS = {
    'Accept': '*/*',
    'X-Youtube-Client-Version': '20.29.3',
    'Authorization': 'Bearer ya29.a0AS3H6Nxad6Nor0fPTsNmKSswCSO_2A_LBwzoqMN5SlNyWtpcsjHXvCOKFa77rK6le-TL0zF0NCz4JEz8r4wXVHvG9NKKm_LBYLHNoyRQk2kIN-KeUSZrfTVDrHa94iE8tU-SXIcS7QQxqQWSgdstGV0RmZI53fK9Tak_2UUkE-dex96qHR8-EsDCKsTJAprtjIKvbXyEZYIqVcAnNExvrNVC7HLYYMPZHuzWnC7BZ9kqBcZQ4nGQyKFp28FF09ghjcgXeI8E7Ej0Zf19E1P98elWaanqhyXkCUSc79d5ElozyM1t1691t2wa30BMeS8955wk3uBipEZH64GiaCgYKAdsSARASFQHGX2MiajvsQeef4JbqG7Bb3g9KIw0343',  # Will be updated by refresh
    'X-Youtube-Client-Name': '5',
    'Accept-Language': 'en-IN,en;q=0.9',
    'Cache-Control': 'no-cache',
    'User-Agent': 'com.google.ios.youtube/20.29.3 (iPad11,1; U; CPU iPadOS 18_5 like Mac OS X; en_IN)',
    'X-GOOG-API-FORMAT-VERSION': '2',
    'X-Goog-Visitor-Id': 'CgtBUkpBNThZdkVUOCiszenDBjIKCgJJThIEGgAgPToMCAEg_LOUnsTVmb1oWMKdxsH92fa5ygE%3D',
    'Connection': 'keep-alive'
}

# Never ever touch this shit
Raw_Youtube_Protobuf_Data = bytes(b'\n\xac\x1e\n\xab\x1a\n\x05en-IN\x12\x02INb\x05Applej\x08iPad11,1\x80\x01\x05\x8a\x01\x0720.29.3\x92\x01\x06iPadOS\x9a\x01\x0c18.5.0.22F76\xca\x01$516761AA-4836-4859-9AA9-DD350062C57C\xa8\x02\x80\x06\xb0\x02\x80\x08\xc8\x02\x02\xf0\x02\x02\xb8\x03\x80\x08\xc0\x03\x80\x06\xe8\x03\x03\xf2\x03\xa9\x12\n\xfa\x08COe1jsQGEOuZrQUQ7bqtBRCJq64FEL22rgUQjteuBRCQ164FENqCsAUQtYqwBRDnpbAFEJzQsAUQz9KwBRCz8rAFEOP4sAUQktSxBRCY2bEFENeYzhwQkrHOHBD8ss4cEIbYzhwQ1NrOHBDL4s4cEOnkzhwQ3ejOHBD-684cEJH1zhwQqITPHBCwhs8cEMKGzxwQ55jPHBCwnc8cEP2ezxwQ1J_PHBDtoc8cEOmjzxwQrKXPHBC2p88cEM6szxwQ3azPHBCkrc8cEKatzxwQ9q3PHBCzrs8cELeuzxwQ9a7PHBCxsM8cELuxzxwQt7XPHBCCt88cEI25zxwQnrvPHBDMu88cEM67zxwQ6rvPHBDRvs8cEOK-zxwQv7_PHBD2wM8cEJLBzxwQycLPHBDFw88cEO_EzxwQ1MXPHBDWxc8cEKjGzxwQtcbPHBC3xs8cEPfGzxwQ-cbPHBCqx88cEKzHzxwQx8jPHBCOyc8cEJbJzxwQmcrPHBCly88cELLLzxwQtcvPHBDFy88cEOLLzxwQ68vPHBDzy88cEPXLzxwQiMzPHBC1zs8cELrQzxwQg9HPHBCR0c8cEM_UzxwaMkFPakZveDJ3a3Jybl9mWjI2b3pWemxDa1ZpRXEyLWtqcmVSTl9lYmlQSkF1YTN0ZHpRIjJBT2pGb3gzMy1VcWdCUHJIVjVydkhXc1NfOFloZWZnUzJLNWpoSk9zSDI5OUo0bTBMQSqwAkNBTVMzUUVOWTRuUXFRTEdFcThSMlhnRzZBTHZCdmdVNndQLUs2TU44aENxQWFRWDZ3RFhLNmdPUnQwTF9RUGRzcHNRNUFhTkQ5d1BwQXJoRDh3THlBdXdEYjhBaEFfYkJMZ0Nxd184QkJTZkFjVUI1Z3lrQmszU0RZTU1oUWJLQWhDSUJtVzVCWUFOMEFZVmNyYXMxZ3lGaFFYbHVRV2tyOUlMRUp3LXV4Zk02d2FrTEliSEJaU3pCdWxwMC1NR3hqSDhsUWFBNkFUZFZwb0IwMTZkRWY0VjdCT1VSLXhlbVlFRzkxR3RKTXg5cVc2ZUlaQjd1MnNBcUNTcE1jOVA0UTJ1RWJnTHNTNzVSdXBfblF2bEFZTm1sWEQ0TXVzRzZndVZDNFlXdVdTX0Z3PT0%3D\x1a\xd0\x04COe1jsQGEhMzMTQ5NTUwMjk3Mjg0NjM1NDQwGOe1jsQGMjJBT2pGb3gyd2tycm5fZloyNm96VnpsQ2tWaUVxMi1ranJlUk5fZWJpUEpBdWEzdGR6UToyQU9qRm94MzMtVXFnQlBySFY1cnZIV3NTXzhZaGVmZ1MySzVqaEpPc0gyOTlKNG0wTEFCsAJDQU1TM1FFTlk0blFxUUxHRXE4UjJYZ0c2QUx2QnZnVTZ3UC1LNk1OOGhDcUFhUVg2d0RYSzZnT1J0MExfUVBkc3BzUTVBYU5EOXdQcEFyaEQ4d0x5QXV3RGI4QWhBX2JCTGdDcXdfOEJCU2ZBY1VCNWd5a0JrM1NEWU1NaFFiS0FoQ0lCbVc1QllBTjBBWVZjcmFzMWd5RmhRWGx1UVdrcjlJTEVKdy11eGZNNndha0xJYkhCWlN6QnVscDAtTUd4akg4bFFhQTZBVGRWcG9CMDE2ZEVmNFY3Qk9VUi14ZW1ZRUc5MUd0Sk14OXFXNmVJWkI3dTJzQXFDU3BNYzlQNFEydUViZ0xzUzc1UnVwX25RdmxBWU5tbFhENE11c0c2Z3VWQzRZV3VXU19Gdz09*\xd6\x04COe1jsQGEhM3MTAxMzk2ODQwOTIzMTM1MjQ1GOe1jsQGKJTk_BIoufX8EiiBgv0SKKXQ_RIonpH-Eiimov4SKMjK_hIooN3-Eii36v4SKKCC_xIowIP_Eiivj_8SKMDd_xIooeD_EijG7v8SKLD1_xIokf7_EijHgIATKIuCgBMo3IKAEyithYATKNiLgBMoppCAEyjLkYATKLGWgBMo2ZaAEyiKl4ATKLiXgBMoxpeAEyjzmoATKKScgBMorZyAEyiLnYATKJqdgBMo052AEyjxnYATKKKegBMo3J6AEyiOn4ATMjJBT2pGb3gyd2tycm5fZloyNm96VnpsQ2tWaUVxMi1ranJlUk5fZWJpUEpBdWEzdGR6UToyQU9qRm94MzMtVXFnQlBySFY1cnZIV3NTXzhZaGVmZ1MySzVqaEpPc0gyOTlKNG0wTEFCcENBTVNUZzBab3RmNkZZZFZfbGpVQ2QwQ2dBNkdOT2tHbkF2eUNya0VrUVN2SnM4QUZTdmR6OElNbXBvSjRiMkxBQ3lsX3NvQzZza0draGFqX1F6UGVOeTRCdU9EQmJwRjFsZWxTcEttQWFYbUJnPT0%3D\x8d\x04\x00\x00\x00@\x98\x04\xca\x02\xe2\x04\x18UICTContentSizeCategoryL\xf0\x04\x02\x82\x05\x0cAsia/Kolkata\xa2\x05\xe5\x05 \x8a\xb3\xed\xe9\x91\xf5\xa0\xe0\xd6\x01 \x86\xd0\x94\xe2\xa8\xb4\xf4\x94\x1a \xd8\xd8\xdc\xc3\xa9\xb6\x9a\xfd\xac\x01 \xac\xe8\x87\xf7\xe0\x86\xda\x95\x8a\x01 \xf3\xfb\x8d\xf4\xcb\xa4\xf5\xe0} \x9f\xaa\xa5\xfb\xfa\xa1\xda\xf6" \xad\xc3\xa0\xf9\x9e\x99\xcf\xa6\xae\x01 \x8b\xa4\xfb\xb6\xd0\xa9\xc7\xa1\xe0\x01 \xf7\x9a\xc1\xfb\xbb\xc7\x9e\xff\x90\x01 \xdb\xc3\xb3\x95\xdf\xc1\xfb\xbb2 \xa1\xe6\xc3\xef\xb4\xb4\x92\xb8\x9a\x01 \x95\xe1\xb3\x93\xb8\xa7\xef\xaf\x81\x01 \xfb\xd4\xfb\xb2\xfb\xbc\xcf\x8aI \xc8\xa3\xff\xaa\xd6\x87\x85\xddN \xa7\xd6\xc0\xcd\xd3\xfd\xca\x8c\xa9\x01 \x98\x8d\xdd\xcd\xb1\xa7\x8b\xe7+ \xdf\x9d\xad\xc4\x81\xf4\x8f\x8e\xa3\x01 \xa2\xb3\xea\xbd\x94\xf5\x84\xd1\x18 \xcc\x80\xe7\x8f\xbc\xf5\x9f\x9bO \x9a\x84\xd3\xf9\xbf\xa8\xb2\xc3\x83\x01 \xa5\xda\xde\xe7\xf3\xf0\xec\xcd\xf0\x01 \x89\xb5\xf6\xe6\xf5\x8e\xd8\x88\x81\x01 \xaa\x83\xb0\xa5\xe9\xfa\xb9\xc7\xb6\x01 \xf9\x85\x92\x95\x9d\xd1\x94\xee\x10 \xc5\xad\xea\xd6\xf2\xb3\xea\xc1\x16 \xbd\xe8\x9f\xd7\xc8\xd5\xab\xb4\xac\x01 \x80\xc2\x80\xf8\xc0\xaf\xc0\xb8\xd5\x01 \xc9\xf0\xd8\xe6\xfa\xfa\x89\x97\\ \xf0\xa6\x92\xb4\x8c\x8e\xc2\x9d5 \xb6\xf5\xfb\x8b\xef\xc7\xd0\xadH \xd2\x88\xdd\x8a\xe3\x95\xb0\x8d\xd7\x01 \x91\xae\xb3\xeb\xe9\xe1\xdf\x92a \xa3\xb5\xaf\xf2\xf1\xd6\xc2\xa9\xca\x01 \xe1\xb6\x90\xfe\x9c\xa1\xa4\xd9\xe4\x01 \xe5\xf6\xa7\xb9\xdb\x95\xdc\xa7\xc6\x01 \xa8\xbe\xd4\x80\xf1\xd4\xc2\x83R \xef\xdb\xa3\x88\xaa\xd3\xcd\xc8\x1f \xf6\x87\xec\xd4\xb1\x8f\xbd\xa8\x82\x01 \x9d\xff\xb0\xb2\x90\x96\xaa\x90Z \x8a\xb0\xac\xcd\xf5\xab\xe3\xd3\xdd\x01 \xa7\xae\xef\x97\x9c\xbe\xcb\xf2\t \xbe\x8a\xce\xe7\x81\xb1\xc3\x9f\xcb\x01 \xeb\xd3\xf3\xbb\xf8\xa9\x94\xb4\x96\x01 \x89\xfb\x82\xaa\xf5\xf1\xae\x86\xb7\x01 \xb9\xe8\xd9\xec\xc9\x93\xe7\xafe \xd1\xa9\xee\x8d\xfb\xb0\xa3\xe0\x0b \xb2\xe8\x96\xe8\x88\xb1\xa4\xdf\xb8\x01 \xf7\xde\xed\xbb\x82\x86\xbb\x86\x08 \xec\xfd\xf4\xd9\xe8\xad\xc9\xff\xe5\x01 \xa2\x9b\xad\xe8\xf4\xba\xc9\xf3W \x81\xb2\xb6\xad\x9a\x96\x94\xc3\x8e\x01 \xa3\xbc\x9b\xb4\x94\xd2\xd8\x9b4 \xe9\x87\xa7\xbf\xf9\x9d\xe5\x85\xa5\x01 \xa5\x98\x98\x8a\xce\xa6\x88\x8c\xc9\x01 \xca\x82\xf4\x9c\xca\x96\xaa\x81\xdd\x01 \xed\xb9\xc4\x88\xb5\x8a\xb7\xc3\xe0\x01 \xe3\xb7\xeb\xe5\xb1\xc5\xad\x8b\xf8\x01 \xf1\xdd\xfb\xfc\xc6\xa3\xe2\xa1\xfd\x01 \xb7\xac\xda\xb4\xa9\x88\x8b\x88\xbc\x01 \xc9\x89\xb1\xb8\xcf\x84\xe1\xc4\xde\x01 \xd4\xdc\x93\xc6\xaf\xdc\xca\xd9e \xe2\xda\xa6\x95\x90\xe1\xcf\x98g \xe5\xf1\xfe\xfc\xf4\xce\xad\x85\xe8\x01 \xf6\x98\xa5\xd0\xae\x91\xae\xd99 \xef\xcf\xef\xf3\xd0\xdd\xe1\x8cI \xe3\x94\xd4\xe1\x94\xcc\x95\xe6\xdd\x01 \xf9\xee\xee\xf3\xbb\xfe\xcb\xca# \xd7\xe1\xea\x91\xd0\x87\x96\xdd\xa8\x01 \xaa\xcd\xf6\x84\xa7\xbb\xc1\x99\xf5\x01 \xb4\xb2\x82\xeb\x9a\xeb\xb9\xa2\xc2\x01\xf8\x05\xae\xff\xba\x01\x8a\x06\x06\x08\x01\x10\x9e\xec$\x9a\x06\x05\n\x03IND\xa2\x06\x07\n\x05\x08\x85M\x18\x01\xa2\x06\x07\n\x05\x08\x85M\x18\x01\xc8\x06\x00\xe2\x06,CPLNwt-l8q2sChC4vayJ28aOAxiTn9Dzo9iOAw%3D%3D\x1a\x00*$\x8a\x02!\x12\x1f\x08\x07\x12\x0e:\x0c\n\x04\x08\x02\x10\x02\n\x04\x08\x04\x10\x03\x18\x80\xa3\x05(\xdd\xc7\x86\xf4\xa3\xd8\x8e\x032E\x12C\x08\xcd\x02\x10\xfcZ\x18\x00"\x13\x08\xd1\x9b\xd0\xf3\xa3\xd8\x8e\x03\x15h\xa0f\x02\x1d\x1c\xd3\'f2\ng-high-recZ\x0fFEwhat_to_watch\x9a\x01\x06\x10\x8e\x1e\x18\xe3\x01J\x8c\x03\n\xdd\x02\n\x02ms\x12\xd6\x02TvSfWH-255_qhhPclmvktUqsV5gU9JcrUja-AXN0Ty9hlUgBWJVGfS79kDg7VGtntwsIInbPBy7hDalX5Vej7McuzoASz3b5qZN70uhOJHVOCuuOQNzxxiLg_QEehLiNZ9yr2YZxEPnylmZrLPGOyX7w33AY0OdXvP8RrUydHcmt78EieoEAaSEvR4s_NkRhgOEqJcZhHeeS0RvSB2pUh4NIEtrAH2ytx0ONaz0rsyrNDZGm7eCFZYX6ZkC2SwsOJGmY23Rk6FLhjHQY9dXxZnMmFgyE7DaVt60SXNW-GA6boZnkqrCD44hMBAAFiufcEJ8XNa_XPdo0M09ZcufQHg0\x02:$00000000-0000-0000-0000-000000000000@\x04H\x01\x12\x0byia4OT9CzaU"Y\nS \x00(\xc3\x010\x008\x03@\x00H\x00X\x00b.video_format=299&sdkv=i.20.29&output=xml_vast2\xe8\x01\x00\xfa\x01\x06:\x04\x18\x00 \x00\xc2\x03\x02\x08\x00\xd2\x03\x00B\x02\x08\x01(\x00@\x00b\x00x\n\xba\x01\x10D9SnwX7YvAFT7T0W\xd2\x01\x02\x08\x00\xda\x01\xa1\x01\n\x9e\x01B\x9b\x01\x00\x05W\xf6\x06\x05\xdf\x05/\xbb\x98\xfez K\x9d\xa5k\xafB#\x96\x05\xd2V\x7f\xe9\xef\x01\x19e\x8e\x98Y3\xd5.T\xa3e!s$\x01\x9b\xc5iT\x00\xf1\xcb\xdd\nd2\x07\xfb\x08@\xf3\xd5\xef\x17pM\xa4]\xcff\xe6\x87\xbc\xa7\xebb\xc8XX\xd2\x160R\x97\xf1\'\xfe\xf5\xdaV\xa9\x1d\xe5o$P\x9d\xcc\x0f\xfb\x16n$\xfc*x\xaf\xfe#\xb0\xcd\xf5\x015\x81\xab|\x84C\x9eR\x0e\x947\xf1L\xd4&\xb1\x87y\\\xdd9\x18\xcd\x01\x83\xf7\x83\x02\x8a\x7f\xb0\xd0\xeea_\xf4\x8d\xa7\x85\x81F\xdc\xe8\xe2\x01\x06\x08\x00\x10\x00\x18\x00')

def extract_video_id(url):
    """Extract video ID from various YouTube URL formats"""
    # Standard URL patterns
    patterns = [
        r"youtube\.com/watch\?v=([^&]+)",         # Standard URL
        r"youtu\.be/([^?]+)",                     # Short URL
        r"youtube\.com/embed/([^/?]+)",           # Embed URL
        r"youtube\.com/v/([^/?]+)",               # Legacy URL
        r"www\.youtube\.com/live/([^/?]+)",       # Live stream URL
        r"youtube\.com/shorts/([^/?]+)",          # Shorts URL
        r"m\.youtube\.com/watch\?v=([^&]+)"       # Mobile URL
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    # Try parsing as URL with query parameters
    parsed = urlparse(url)
    if parsed.hostname and ('youtube.com' in parsed.hostname or 'youtu.be' in parsed.hostname):
        if parsed.path.startswith('/watch'):
            qs = parse_qs(parsed.query)
            if 'v' in qs:
                return qs['v'][0]
        elif parsed.path.startswith('/'):
            return parsed.path[1:].split('?')[0].split('/')[0]
    
    return None

def add_seconds_to_time_range_with_rejection(start_time_str, end_time_str, seconds_to_add):
    dummy_date = datetime(2000, 1, 1).date()
    
        # Parse the input time strings into datetime objects
    start_datetime_obj = datetime.combine(dummy_date, datetime.strptime(start_time_str, '%H:%M:%S').time())
    end_datetime_obj = datetime.combine(dummy_date, datetime.strptime(end_time_str, '%H:%M:%S').time())
    
        # Create a timedelta object for the seconds to add
    time_delta = timedelta(seconds=seconds_to_add)
    
        # Define the rejection threshold (00:00:05)
    rejection_threshold_time = datetime.strptime("00:00:05", '%H:%M:%S').time()
    rejection_threshold_datetime = datetime.combine(dummy_date, rejection_threshold_time)
    
    new_start_time_str = start_time_str # Initialize with original start time
    new_end_time_str = (end_datetime_obj + time_delta).strftime('%H:%M:%S') # Always add to end time
    
        # Apply the rejection logic for start time
    if start_datetime_obj <= rejection_threshold_datetime:
        print(f"INFO: Start time '{start_time_str}' is 00:00:00 or less than/equal to 00:00:05. "
                  "Not adding seconds to start time.")
            # new_start_time_str remains the original start_time_str as initialized
    else:
        new_start_time_str = (start_datetime_obj - time_delta).strftime('%H:%M:%S')
    
    return new_start_time_str, new_end_time_str
    
class Proto:
    def __init__(self, disassembled_data: str):
        self.original_data = disassembled_data
        self.modified_data = disassembled_data
        self.ps = ProtoFuck()

    def modify_field(self, field_path: tuple, new_value: str) -> None:
        if len(field_path) == 1:
            self._modify_top_level_field(field_path[0], new_value)
        else:
            self._modify_nested_field(field_path, new_value)

    def _modify_top_level_field(self, field_num: int, new_value: str) -> None:
        pattern = rf'^({field_num}:\s*{{)"[^"]*"}}(.*)$'
        modified_lines = []
        for line in self.modified_data.split('\n'):
            match = re.match(pattern, line)
            if match:
                new_line = f'{match.group(1)}"{new_value}"}}{match.group(2)}'
                modified_lines.append(new_line)
            else:
                modified_lines.append(line)
        self.modified_data = '\n'.join(modified_lines)

    def _modify_nested_field(self, field_path: tuple, new_value: str) -> None:
        indent = ' ' * (2 * (len(field_path) - 1))
        pattern = rf'^({indent}{field_path[-1]}:\s*{{)"[^"]*"}}(.*)$'
        modified_lines = []
        for line in self.modified_data.split('\n'):
            match = re.match(pattern, line)
            if match:
                new_line = f'{match.group(1)}"{new_value}"}}{match.group(2)}'
                modified_lines.append(new_line)
            else:
                modified_lines.append(line)
        self.modified_data = '\n'.join(modified_lines)
        
    def assemble(self) -> bytes:
        return self.ps.assemble(self.modified_data)
        
    def disassemble(self) -> bytes:
        return self.ps.disassemble(self.original_data)
        
    def reset(self) -> None:
        self.modified_data = self.original_data
        
    def get_modified_text(self) -> str:
        return self.modified_data

#Never touch this below Regex shit too
def extract_quoted_strings(text):
    matches = re.findall(r'"([^"]*)"', text)
    return "".join(matches)

def parse_protobuf_snippet(snippet):
    parsed_data = {}
    match = re.search(r'1: \{"([^"]+)"\}', snippet)
    if match:
        parsed_data['video_id'] = match.group(1)
    match = re.search(r'15: \{\s*((?:"[^"]*"\s*)+)\}', snippet, re.DOTALL)
    if match:
        parsed_data['video_title'] = extract_quoted_strings(match.group(1))
    match = re.search(r'16: (\d+)', snippet)
    if match:
        parsed_data['duration_seconds'] = int(match.group(1))
    match = re.search(r'18: \{"([^"]+)"\}', snippet)
    if match:
        parsed_data['test_field'] = match.group(1)
    match = re.search(r'19: \{"([^"]+)"\}', snippet)
    if match:
        parsed_data['channel_id'] = match.group(1)
    match = re.search(r'21: \{\s*((?:"[^"]*"\s*)+)\}', snippet, re.DOTALL)
    if match:
        parsed_data['description'] = extract_quoted_strings(match.group(1))
    for key in [20, 22, 31, 37, 38, 41]:
        match = re.search(rf'{key}: (\d+)', snippet)
        if match:
            parsed_data[f'field_{key}'] = int(match.group(1))
    match = re.search(r'32: \{"([^"]+)"\}', snippet)
    if match:
        parsed_data['field_32'] = match.group(1)
    thumbnails_data = {'hq': {}, 'sd': {}}
    thumbnails_block_match = re.search(r'25: \{((?:.|\n)*?)\n\s*\}', snippet, re.DOTALL)
    if thumbnails_block_match:
        thumbnails_content = thumbnails_block_match.group(1)
        hq_match = re.search(r'1: \{\s*1: \{\s*"([^"]+)"\s*\}\s*2: (\d+)\s*3: (\d+)\s*\}', thumbnails_content, re.DOTALL)
        if hq_match:
            thumbnails_data['hq']['url'] = hq_match.group(1)
            thumbnails_data['hq']['width'] = int(hq_match.group(2))
            thumbnails_data['hq']['height'] = int(hq_match.group(3))
        sd_match = re.search(
            r'(?:1: \{\s*1: \{\s*"[^"]+"\s*\}\s*2: \d+\s*3: \d+\s*\}){1}\s*'
            r'1: \{\s*1: \{\s*"([^"]+)"\s*\}\s*2: (\d+)\s*3: (\d+)\s*\}',
            thumbnails_content,
            re.DOTALL
        )
        if sd_match:
            thumbnails_data['sd']['url'] = sd_match.group(1)
            thumbnails_data['sd']['width'] = int(sd_match.group(2))
            thumbnails_data['sd']['height'] = int(sd_match.group(3))
    parsed_data['thumbnails'] = thumbnails_data
    return parsed_data
    
def get_youtube_hls(video_id):
    try:
        rawBytes = Proto(Raw_Youtube_Protobuf_Data)
        data = Proto(rawBytes.disassemble())
        data.modify_field((2,), video_id)
        binary_data = data.assemble()
        url = "https://youtubei.googleapis.com/youtubei/v1/player"
        params = {
            "id": f"{video_id}",
            "t": "9029A1C6-BD84-4D2C-8DDA-A48BF26A6005"
        }
        YT_HEADERS['Host'] = 'youtubei.googleapis.com'
        YT_HEADERS['Content-Type'] = 'application/x-protobuf'
        response = requests.post(
            url,
            params=params,
            headers=YT_HEADERS,
            data=binary_data
        )
        resp = response.content
        d_ = Proto(resp)
        dd_ = Proto(d_.disassemble())
        extracted_data = parse_protobuf_snippet(re.findall(r"11: {\n.*22: 1", dd_.get_modified_text(), re.S)[0])
        logging.error(f"META INFO  @ get_youtube_hls : {json.dumps(extracted_data, indent=4)}")
        return {
            'title': extracted_data["video_title"],
            'duration': extracted_data["duration_seconds"],
            'hls': re.findall(r'https://manifest.googlevideo.com/.*?m3u8', resp.decode('utf-8', errors='replace'))[0]
        }
    except Exception as a:
        logging.error(f"An error occurred  @ get_youtube_hls : {a}")
        logging.error(f"Type of error  @  get_youtube_hls : {type(a)}")
        traceback.print_exc()

# Updated download_thread function
def download_thread(link, ss, to, output, format, request_id):
    try:
        seconds_to_add = 5
        new_start_time, new_end_time = add_seconds_to_time_range_with_rejection(ss, to, 5)     
        # Extract video ID
        video_id = extract_video_id(link)
        if not video_id:
            raise Exception("Invalid YouTube URL")
        meta = get_youtube_hls(video_id)
        title = meta['title']
        hls = meta['hls']
        
        # Generate output filename
        if not output:
            base = re.sub(r'[\\/*?:"<>|\x00-\x1f]', '', title).strip()
            base = re.sub(r'\s+', ' ', base)
            if len(base) > 200:  
                base = base[:200] or "clip"
            output = f"{base}.{format}"
        else:
            base = output.rsplit('.', 1)[0] if '.' in output else output
            output = f"{base}.{format}"
        safe_outputt = output
        
        logging.error(f"SAFE OUTPUT: {safe_outputt}")
        decoded_filename = unquote(safe_outputt)
        safe_output = secure_filename(decoded_filename.split('/')[-1])
        output_path = os.path.join(app.config['DOWNLOAD_FOLDER'], safe_output)
        
        # Update status
        with download_lock:
            download_status[request_id] = {
                'status': 'downloading',
                'progress': 0,
                'filename': safe_output,
                'message': 'Starting download...'
            }
        
        # Create temp file name (always MKV for initial download)
        temp_filename = f"{safe_output.rsplit('.', 1)[0]}.mkv"
        temp_path = os.path.join(app.config['DOWNLOAD_FOLDER'], temp_filename)
        
        # Delete temp file if it exists
        if os.path.exists(temp_path):
            os.remove(temp_path)
        #re_path = os.path.abspath("N_m3u8DL-RE")
        cmd = [
            "N_m3u8DL-RE",
            "--no-ansi-color",
            "--custom-range", f"{new_start_time}-{new_end_time}",
            "-M", "format=mkv:muxer=mkvmerge",
            "--concurrent-download",
            hls,
            "--drop-video", "PQ",
            "--select-video", "best",
            "--select-audio", "best",
            "--drop-subtitle", "en",
            "--save-name", temp_filename.rsplit('.', 1)[0],
            "--save-dir", app.config['DOWNLOAD_FOLDER'],
            "--log-level", "INFO"
        ]
        
        # Run command
        process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )
        
        # Regular expressions to parse progress
        vid_progress_regex = re.compile(
            r'Vid.*?\s+(\d+)/(\d+)\s+(\d+\.\d+)%'
        )
        aud_progress_regex = re.compile(
            r'Aud.*?\s+(\d+)/(\d+)\s+(\d+\.\d+)%'
        )
        done_regex = re.compile(r'Done|Muxing to')
        
        # Track progress for video and audio separately
        vid_progress = 0
        aud_progress = 0
        total_segments = 5  # Default, will be updated from actual output
        merging = False
        
        for line in iter(process.stdout.readline, ''):
            line = line.strip()
            logging.info(f"N_m3u8DL-RE: {line}")
            
            # Check for video progress
            vid_match = vid_progress_regex.search(line)
            if vid_match:
                current, total, percent = vid_match.groups()
                total_segments = int(total)
                vid_progress = float(percent)
                current_progress = (vid_progress + aud_progress) / 2  # Average of both
                
                with download_lock:
                    download_status[request_id] = {
                        'status': 'downloading',
                        'progress': current_progress * 0.9,  # Scale to 0-90%
                        'filename': safe_output,
                        'message': f'Downloading {current_progress:.1f}% (V:{vid_progress:.1f}% A:{aud_progress:.1f}%)'
                    }
            
            # Check for audio progress
            aud_match = aud_progress_regex.search(line)
            if aud_match:
                current, total, percent = aud_match.groups()
                aud_progress = float(percent)
                current_progress = (vid_progress + aud_progress) / 2  # Average of both
                
                with download_lock:
                    download_status[request_id] = {
                        'status': 'downloading',
                        'progress': current_progress * 0.9,  # Scale to 0-90%
                        'filename': safe_output,
                        'message': f'Downloading {current_progress:.1f}% (V:{vid_progress:.1f}% A:{aud_progress:.1f}%)'
                    }
            
            # Check for merging phase
            if "Muxing to" in line and not merging:
                merging = True
                with download_lock:
                    download_status[request_id] = {
                        'status': 'merging',
                        'progress': 90,
                        'filename': safe_output,
                        'message': 'Merging video and audio...'
                    }
            
            # Check for completion
            if done_regex.search(line) and merging:
                with download_lock:
                    download_status[request_id] = {
                        'status': 'merging',
                        'progress': 95,
                        'filename': safe_output,
                        'message': 'Finalizing output file...'
                    }
                break
        
        process.wait() 
        
        if process.returncode != 0:
            raise Exception(f"N_m3u8DL-RE failed with code {process.returncode}")
        
        if not os.path.exists(temp_path):
            raise Exception("Downloaded file not found")
        
        # Handle format conversion if needed
        if format != 'mkv' and os.path.exists(temp_path):
            conversion_stage = 'converting' if format == 'mp4' else 'extracting'
            
            with download_lock:
                download_status[request_id] = {
                    'status': conversion_stage,
                    'progress': 95,
                    'filename': safe_output,
                    'message': f'Converting to {format.upper()}...'
                }
            
            # Track conversion progress by monitoring output file growth
            if os.path.exists(output_path):
                os.remove(output_path)
            
            if format == 'mp4':
                # Convert MKV to MP4 using FFmpeg
                ffmpeg_cmd = [
                    'ffmpeg',
                    '-i', temp_path,
                    '-c', 'copy',  # Copy all streams without re-encoding
                    output_path,
                    '-y'  # Overwrite output file if exists
                ]
            elif format == 'aac':
                # Extract audio to AAC
                ffmpeg_cmd = [
                    'ffmpeg',
                    '-i', temp_path,
                    '-vn',          # No video
                    '-acodec', 'copy',  # Copy audio without re-encoding
                    output_path,
                    '-y'
                ]
            
            ffmpeg_process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )
            
            # Monitor conversion progress
            start_time = time.time()
            last_update = time.time()
            
            while ffmpeg_process.poll() is None:
                time.sleep(0.5)
                
                # Simple time-based progress for conversion (95-100%)
                elapsed = time.time() - start_time
                progress = 95 + min(5, elapsed * 2)  # Conversion should be fast
                
                # Only update every 0.5 seconds to avoid spamming
                if time.time() - last_update > 0.5:
                    last_update = time.time()
                    with download_lock:
                        download_status[request_id] = {
                            'status': conversion_stage,
                            'progress': progress,
                            'filename': safe_output,
                            'message': 'Converting...'
                        }
            
            # Check if conversion succeeded
            if ffmpeg_process.returncode != 0:
                raise Exception(f"FFmpeg failed with code {ffmpeg_process.returncode}")
            
            # Remove temp file
            os.remove(temp_path)
            
            if not os.path.exists(output_path):
                raise Exception("Converted file not created")
        
        # Final success update
        with download_lock:
            download_status[request_id] = {
                'status': 'completed',
                'progress': 100,
                'filename': safe_output,
                'message': 'Ready to download!'
            }
            
    except Exception as e:
        logging.error(f"Download error: {str(e)}")
        traceback.print_exc()
        with download_lock:
            download_status[request_id] = {
                'status': 'error',
                'progress': 0,
                'filename': output if output else "clip",
                'message': f'Error: {str(e)}'
            }

@app.route("/video_info/<video_id>")
def get_video_info(video_id):
    try:
        stream = get_youtube_hls(video_id)
        return jsonify({
            "stream_url": stream['hls'],
            "duration": stream['duration'],
            "title": stream['title']
        })
        
    except Exception as c:
        logging.error(f"Error getting video info: {str(c)}")
        logging.ERROR(f"Type of error: {type(c)}")
        traceback.print_exc()
        return jsonify({"error": str(c)}), 500

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "GET":
        return render_template("index.html")
    
    # Handle both form and JSON requests
    if request.is_json:
        data = request.get_json()
        link = data.get("link", "").strip()
        ss = data.get("ss", "").strip()
        to = data.get("to", "").strip()
        output = data.get("output", "").strip()
        format = data.get("format", "mp4").strip()
    else:
        link = request.form.get("link", "").strip()
        ss = request.form.get("ss", "").strip()
        to = request.form.get("to", "").strip()
        output = request.form.get("output", "").strip()
        format = request.form.get("format", "mp4").strip()
    
    # Validate inputs
    errors = []
    if not link:
        errors.append("Please enter a YouTube URL")
    elif not extract_video_id(link):
        errors.append("Invalid YouTube URL format")
    if not validate_time(ss):
        errors.append("Invalid start time format (use HH:MM:SS or MM:SS)")
    if not validate_time(to):
        errors.append("Invalid end time format (use HH:MM:SS or MM:SS)")
    
    # Validate format
    valid_formats = ['mp4', 'mkv', 'aac']
    if format not in valid_formats:
        format = 'mp4'  # Default to mp4 if invalid
    
    # Calculate duration in seconds
    if not errors:
        try:
            start_sec = time_to_seconds(ss) if ss else 0
            end_sec = time_to_seconds(to) if to else 300  # Default to 5 minutes
            
            if end_sec <= start_sec:
                errors.append("End time must be after start time")
            elif end_sec - start_sec > 300:
                errors.append("Clip duration cannot exceed 5 minutes")
        except Exception as e:
            errors.append(f"Error processing times: {str(e)}")
    
    if errors:
        if request.is_json:
            return jsonify({"errors": errors}), 400
        else:
            for error in errors:
                flash(error, "error")
            return redirect(url_for("index"))
    
    # Generate unique request ID
    request_id = os.urandom(16).hex()
    
    # Start download in background thread
    thread = threading.Thread(
        target=download_thread,
        args=(link, ss or "00:00:00", to or "00:05:00", output, format, request_id)
    )
    thread.start()
    
    if request.is_json:
        return jsonify({
            "request_id": request_id,
            "filename": output  # Note: This will be updated in the thread
        })
    else:
        return redirect(url_for("download_status_page", request_id=request_id))

@app.route("/stream/<filename>")
def stream_file(filename):
    decoded_filename = unquote(filename)
    safe_filename = secure_filename(decoded_filename.split('/')[-1])
    file_path = os.path.join(app.config['DOWNLOAD_FOLDER'], safe_filename)
    
    if not os.path.exists(file_path):
        return "File not found", 404

    file_size = os.path.getsize(file_path)
    range_header = request.headers.get('Range', None)
    
    # Determine MIME type based on file extension
    ext = os.path.splitext(filename)[1].lower()
    mime_types = {
        '.mp4': 'video/mp4',
        '.webm': 'video/webm',
        '.mkv': 'video/x-matroska'
    }
    mime_type = mime_types.get(ext, 'video/mp4')

    if not range_header:
        # Initial request - return the whole file with streaming headers
        response = Response(
            file_iterator(file_path),
            status=200,
            mimetype=mime_type,
            direct_passthrough=True
        )
        response.headers['Content-Length'] = file_size
        response.headers['Accept-Ranges'] = 'bytes'
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response

    # Parse range header
    start, end = parse_range_header(range_header, file_size)
    content_length = end - start + 1

    # Create response with partial content
    response = Response(
        file_iterator(file_path, start, end),
        status=206,
        mimetype=mime_type,
        direct_passthrough=True
    )
    
    response.headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
    response.headers['Content-Length'] = content_length
    response.headers['Accept-Ranges'] = 'bytes'
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    
    return response

def parse_range_header(range_header, file_size):
    """Parse range header and return start/end bytes"""
    unit, ranges = range_header.split('=')
    if unit != 'bytes' or ',' in ranges:
        raise ValueError('Invalid range header')
    
    start, end = ranges.split('-')
    start = int(start) if start else 0
    end = int(end) if end else file_size - 1
    
    # Ensure end isn't beyond file size
    end = min(end, file_size - 1)
    return start, end

def file_iterator(file_path, start=None, end=None, chunk_size=1024*1024):
    """Generator function to stream file in chunks"""
    with open(file_path, 'rb') as f:
        if start is not None:
            f.seek(start)
        
        remaining = None
        if end is not None:
            remaining = end - start + 1
        
        while True:
            if remaining is not None and remaining <= 0:
                break
                
            bytes_to_read = min(chunk_size, remaining) if remaining is not None else chunk_size
            data = f.read(bytes_to_read)
            
            if not data:
                break
                
            if remaining is not None:
                remaining -= len(data)
                
            yield data
            
@app.route("/progress/<request_id>")
def download_progress(request_id):
    with download_lock:
        status = download_status.get(request_id, {
            'status': 'unknown',
            'progress': 0,
            'filename': '',
            'message': 'Sent forged Protobuf data to Youtube'
        })
    return jsonify(status)

@app.route("/download/<path:filename>")
def download_file(filename):
    # Decode URL-encoded filename
    decoded_filename = unquote(filename)
    safe_filename = secure_filename(decoded_filename.split('/')[-1])
    logging.error(safe_filename)
    file_path = os.path.join(app.config['DOWNLOAD_FOLDER'], safe_filename)
    logging.error(file_path)
    
    if not os.path.exists(file_path):
        return "File not found or has expired", 404
    
    return send_file(file_path, as_attachment=True)

@app.route("/download_status/<request_id>")
def download_status_page(request_id):
    return render_template("download_status.html", request_id=request_id)

def cleanup_scheduler():
    """Run cleanup every minute"""
    while True:
        cleanup_old_files()
        time.sleep(60)  # Run every minute

# In the main block, start the token refresh thread
if __name__ == "__main__":
    # Initial token refresh
    refresh_youtube_token()
    
    # Start token refresh thread
    token_thread = threading.Thread(target=token_refresh_scheduler, daemon=True)
    token_thread.start()
    
    # Existing cleanup thread
    cleanup_thread = threading.Thread(target=cleanup_scheduler, daemon=True)
    cleanup_thread.start()
    
    port = int(os.environ.get("PORT", 45638))
    app.run(debug=True, host='0.0.0.0', port=port)

