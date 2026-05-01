import socket
import ssl
import json
import threading
import time
import sys
import os 
import zlib 
import select 
import struct
import configparser
import concurrent.futures
from typing import Dict, Any, Optional, Tuple, Union, List

try:
    from zeroconf import ServiceBrowser, Zeroconf, ServiceStateChange
    HAS_ZEROCONF = True
except ImportError:
    HAS_ZEROCONF = False

try:
    # msvcrt (Microsoft Visual C Runtime) only on Windows.
    import msvcrt
    import winsound
    IS_WINDOWS = True
except ImportError:
    IS_WINDOWS = False
    import termios
    import tty

# --- CONTANTS ---
PRINTER_PORT_SECURE = 12309

PRINTER_IP = ""
USERNAME = ""
LOCAL_CODE = ""

pause = False
FIRSTRUN=False

LOCAL_FILE_PATH = "print.makerbot" 
RPC_BLOCK_SIZE = 131072  # 128 KB block size: kb * 1024
RPC_FILE_ID = 'AAAA'    # File id
RPC_FILE_PATH = f"//current_thing/{os.path.basename(LOCAL_FILE_PATH)}"

ssl_socket: Optional[ssl.SSLSocket] = None
global_request = 1
is_running = threading.Event()

async_messages = []
async_lock = threading.Lock() 

printer_status: Dict[str, Any] = {
    "process": "IDLE",
    "step": "N/A",    
    "filename": "N/A", 
    "progress": "N/A",        
    "elapsed_time": "N/A",    
    "chamber_current": "N/A",
    "chamber_target": "N/A",
    "extruder_current": "N/A",
    "extruder_target": "N/A",
    "preheating": False
}

last_action_feedback: str = ""
last_feedback_time: float = 0.0
FEEDBACK_DURATION = 15.0 
status_lock = threading.Lock() 

# --- Hardcoded JSON-RPC COMMANDS  ---

JSON_menu_1 = '{"params": {}, "jsonrpc": "2.0", "method": "print_again"}'
JSON_menu_2 = '{"params": {}, "jsonrpc": "2.0", "method": "preheat"}'
JSON_menu_3 = '{"params": {"temperature_settings": 215, "tool_index": 0}, "jsonrpc": "2.0", "method": "load_filament"}'
JSON_menu_4 = '{"params": {"temperature_settings": 215, "tool_index": 0}, "jsonrpc": "2.0", "method": "unload_filament"}'
JSON_menu_5 = '{"params": {}, "jsonrpc": "2.0", "method": "cool"}'
JSON_menu_6 = '{"params": {}, "jsonrpc": "2.0", "method": "park"}'

JSON_menu_8 = '{"params": {"machine_func": "set_temperature_target", "params": {"index": 0, "temperature": 280}}, "jsonrpc": "2.0", "method": "machine_action_command"}'
JSON_menu_9 = '{"params": {"index":0}, "jsonrpc": "2.0", "method": "load_print_tool"}'

JSON_menu_x = '{"params": {}, "jsonrpc": "2.0", "method": "cancel"}'
JSON_menu_enter_1 = '{"params": {"method":"acknowledge_error"}, "jsonrpc": "2.0", "method": "process_method"}'
JSON_menu_enter_2 = '{"params": {"method":"acknowledge_failure"}, "jsonrpc": "2.0", "method": "process_method"}'
JSON_menu_enter_3 = '{"params": {"method":"acknowledge_completed"}, "jsonrpc": "2.0", "method": "process_method"}'

JSON_menu_space_1 = '{"params": {"method":"suspend"}, "jsonrpc": "2.0", "method": "process_method"}'
JSON_menu_space_2 = '{"params": {"method":"resume"}, "jsonrpc": "2.0", "method": "process_method"}'

# ==============================================================================
#           LISTENER
# ==============================================================================
class ListenerThread(threading.Thread):
    
    def __init__(self, sock: ssl.SSLSocket):
        super().__init__()
        self.sock = sock
        self.running = True
        self.daemon = True 
        self.last_status_time = time.time()

    def read_json_response(self, socket_obj: socket.socket) -> dict | None:
        if not hasattr(self, 'buffer'):
            self.buffer = b''

        while True:
            try:
                chunk = socket_obj.recv(4096)
                if not chunk:
                    print("Error: The connection to the server was closed while reading.")
                    return None
                
                self.buffer += chunk
                
                json_start = self.buffer.find(b'{')
                if json_start == -1:
                    continue

                temp_data = self.buffer[json_start:].decode('utf-8')
                
                try:
                    response = json.loads(temp_data)
                    
                    json_bytes = json.dumps(response).encode('utf-8')
                    json_size = len(json_bytes)
                    
                    self.buffer = self.buffer[json_start + json_size:]
                    return response
                    
                except json.JSONDecodeError:
                    pass 
                
            except socket.timeout:
                return None
            except Exception as e:
                print(f"Unexpected error while reading: {e}")
                return None

    def run(self):
        print("\n--- LISTENER START: I am listening for messages from the printer... ---")
        self.sock.settimeout(1.0)
        
        while is_running.is_set():
            try:
                json_response = self.read_json_response(self.sock)
                
                if json_response is None:
                    if not is_running.is_set():
                        break
                    continue 

                if 'id' in json_response:                                        
                    print(f"\033[92m Response:", json_response, "\033[0m")
                    pass
                    
                elif 'method' in json_response:                      
                    method = json_response.get('method', 'ismeretlen.metódus')
                    params = json_response.get('params', {})
                    if method in ["system_notification", "state_nothification"]:
                        #print(f"\033[93m Notification:", json_response, "\033[0m")
                        if method == "system_notification" and 'info' in params:
                            self.update_printer_status(params['info'])
                        else:
                            self.update_printer_status(params)
                    else:
                        pass
                        
            except Exception as e:
                if is_running.is_set():
                    print(f"\n\n❌ **ERROR in Listener Thread**: {e}")
                    
        
        print("------ 🛑 LISTENER HAS BEEN STOPPED ------")
                    

    def update_printer_status(self, params: Dict[str, Any]):
        with status_lock:
            current_process_data = params.get("current_process")
            current_filename = "N/A"
            current_progress = "N/A"
            current_elapsed_time = "N/A"
            current_process_name = "IDLE"
            current_step = "N/A"

            if isinstance(current_process_data, dict):
                current_process_name = current_process_data.get("name", "Active process")
                current_step = current_process_data.get("step", "N/A")                
                if current_step == "completed": 
                    if IS_WINDOWS:
                        winsound.Beep(784, 600)
                        winsound.Beep(880, 600)
                
                filename = current_process_data.get("filename") 
                if filename:
                     current_filename = filename.split('/')[-1]
                
                # Progress
                progress = current_process_data.get("progress")
                current_progress = f"{progress}%" if progress is not None else "N/A"
                
                # Elapsed time
                elapsed = current_process_data.get("elapsed_time")
                if isinstance(elapsed, (int, float)):
                    hours, remainder = divmod(int(elapsed), 3600)
                    minutes, seconds = divmod(remainder, 60)
                    current_elapsed_time = f"{hours:02}:{minutes:02}:{seconds:02}"

            printer_status["process"] = current_process_name
            printer_status["step"] = current_step
            printer_status["filename"] = current_filename
            printer_status["progress"] = current_progress
            printer_status["elapsed_time"] = current_elapsed_time
            
            if "chamber_temp" in params:
                c_temp = params.get("chamber_temp", {})
                printer_status["chamber_current"] = c_temp.get("current", printer_status["chamber_current"])
                printer_status["chamber_target"] = c_temp.get("target", printer_status["chamber_target"])

                e_temp = params.get("extruder_temp", {})
                printer_status["extruder_current"] = e_temp.get("current", printer_status["extruder_current"])
                printer_status["extruder_target"] = e_temp.get("target", printer_status["extruder_target"])

                printer_status["preheating"] = params.get("is_menu_2ing", printer_status["preheating"])
            
            if "toolheads" in params:
                toolheads = params["toolheads"]
                
                if 'chamber' in toolheads and isinstance(toolheads['chamber'], list) and toolheads['chamber']:
                    chamber_data = toolheads['chamber'][0]
                    printer_status["chamber_current"] = chamber_data.get("current_temperature", printer_status["chamber_current"])
                    printer_status["chamber_target"] = chamber_data.get("target_temperature", printer_status["chamber_target"])
                    if 'preheating' in chamber_data:
                        printer_status["preheating"] = chamber_data["preheating"]
                    
                if 'extruder' in toolheads and isinstance(toolheads['extruder'], list) and toolheads['extruder']:
                    extruder_data = toolheads['extruder'][0]
                    printer_status["extruder_current"] = extruder_data.get("current_temperature", printer_status["extruder_current"])
                    printer_status["extruder_target"] = extruder_data.get("target_temperature", printer_status["extruder_target"])
                    
                    if 'preheating' in extruder_data and extruder_data['preheating']:
                           printer_status["preheating"] = True

# ==============================================================================
#                 RPC UTILITIES AND CONTROL
# ==============================================================================

def rpc_call_raw(raw_json_string: str) -> Tuple[Optional[str], Optional[str]]:
    global ssl_socket, global_request
    if not ssl_socket:
        return None, "The socket is not initialized."
    try:
        rpc_data = json.loads(raw_json_string)
        rpc_data["id"] = global_request
        method_name = rpc_data.get("method", "unknown.method")
        final_payload = json.dumps(rpc_data)
        print(final_payload)
        ssl_socket.sendall(final_payload.encode('utf-8'))
        global_request += 1
        return method_name, None 
        
    except json.JSONDecodeError:
        return None, "The hardcoded JSON string is invalid (program error)."
    except Exception as e:
        return None, str(e)

# ==============================================================================
#           RPC FILE UPLOAD LOGIC
# ==============================================================================

def rpc_file_upload(upload_file_path=None) -> Tuple[Optional[str], Optional[str]]:
    global ssl_socket, global_request, filename
    
    if upload_file_path is None:
        upload_file_path = LOCAL_FILE_PATH

    rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "clear_queue"}')
    time.sleep(0.1)
    rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "close_queue"}')
    time.sleep(0.1)
    
    if os.path.isabs(upload_file_path):
        absolute_local_path = upload_file_path
    else:
        absolute_local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), upload_file_path)
    
    if not os.path.exists(absolute_local_path):
        return "rpc_file_upload", f"ERROR: Local file not found: {absolute_local_path}"
    try:
        with open(absolute_local_path, 'rb') as f:
            file_bytes = f.read()
            file_size = len(file_bytes)
            crc_checksum = zlib.crc32(file_bytes) & 0xFFFFFFFF
    except Exception as e:
        return "rpc_file_upload", f"ERROR: Error reading local file: {e}"

    feedback_prefix = f"Upload ({os.path.basename(absolute_local_path)}, {file_size} byte)"

    printer_file_name = os.path.basename(absolute_local_path)
    current_rpc_file_path = f"//current_thing/{printer_file_name}"

    # --- 1. STEP: put_init    
    init_params = {
        "length": file_size, 
        "block_size": RPC_BLOCK_SIZE, 
        "file_path": current_rpc_file_path,
        "file_id": RPC_FILE_ID
    }
    call = {
        "params": init_params,
        "jsonrpc": "2.0",
        "method": "put_init",
        "id": global_request
    } 
    json_string = json.dumps(call)               
    payload = json_string.encode('utf-8')
    print(payload)
    ssl_socket.sendall(payload)
    global_request += 1
    time.sleep(3)
     
    # --- 2. STEP: put_raw  ---
    bytes_sent = 0
    i=0
    print("\n Upload is starting.")
    while bytes_sent < file_size:
        chunk = file_bytes[bytes_sent:bytes_sent + RPC_BLOCK_SIZE]    
        chunk_len = len(chunk)
        i=int((bytes_sent/file_size)*100)
        raw_params_list = [RPC_FILE_ID, chunk_len]
        raw_request = {
            "params": raw_params_list,
            "jsonrpc": "2.0",
            "method": "put_raw",
            "id": global_request
        }  
        raw_header = json.dumps(raw_request)
        payload = raw_header.encode('utf-8')
        print(payload)
        ssl_socket.sendall(payload)
        print(f"\r",i, "% ",int(0.5*i)*".", end='', flush=True)
        ssl_socket.sendall(chunk)
        global_request += 1
        bytes_sent += chunk_len
        time.sleep(1)
    
    # --- 3. STEP: put_term  --- 
    print("\n Finished")
    term_params = {
        "crc": crc_checksum,   
        "length": file_size,
        "file_id": RPC_FILE_ID
    }
    call = {
        "params": term_params,
        "jsonrpc": "2.0",
        "method": "put_term",
        "id": global_request              
    }          
    json_string = json.dumps(call)               
    payload = json_string.encode('utf-8')
    print(payload)
    ssl_socket.sendall(payload)
    global_request += 1
    
    print_params = {
        "filepath": printer_file_name,
        "ensure_build_plate_clear": False
    } 
    
    call = {
        "params": print_params,
        "jsonrpc": "2.0",
        "method": "print",
        "id": global_request              
    }          
    
    json_string = json.dumps(call)               
    payload = json_string.encode('utf-8')
    ssl_socket.sendall(payload)
    global_request += 1
    return "Upload and print",""

def home_xy():
    try:
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "close_queue"}')
        rpc_call_raw('{"params": {"clear": true}, "jsonrpc": "2.0", "method": "open_queue"}')
        rpc_call_raw('{"params": {"machine_func": "home_axis", "params": {"axis": 1, "speed": 11, "flip_direction": true, "set_position": false}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {"machine_func": "set_position", "params": {"axis": 1, "position_mm": -152}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {"machine_func": "move_axis", "params": {"axis": 1, "point_mm": 0, "mm_per_second": 100, "relative":false}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {"machine_func": "home_axis", "params": {"axis": 0, "speed": 11, "flip_direction": false, "set_position": true}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {"machine_func": "move", "params": {"point_mm":[145.5, 0, 0, 0], "mm_per_second":100.0, "relative":[false, true, true, true]}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {"machine_func": "home_axis", "params": {"axis": 1, "speed": 11, "flip_direction": false, "set_position": true}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {"machine_func": "move", "params": {"point_mm":[0, 175, 0, 0], "mm_per_second":100.0, "relative":[true, false, true, true]}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "execute_queue"}')
        return "Home X/Y process", None 
    except Exception as e:
        return "There is something wrong with my HOME X/Y commands.", str(e)

def home_z():
    try:
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "close_queue"}')
        rpc_call_raw('{"params": {"machine_func": "set_temperature_target", "params": {"index": 0, "temperature": 180}}, "jsonrpc": "2.0", "method": "machine_action_command"}')
        rpc_call_raw('{"params": {"clear": true}, "jsonrpc": "2.0", "method": "open_queue"}')
        rpc_call_raw('{"params": {"machine_func": "home_axis", "params": {"axis": 0, "speed": 30, "flip_direction": true, "set_position": true}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {"machine_func": "home_axis", "params": {"axis": 1, "speed": 30, "flip_direction": false, "set_position": true}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {"machine_func": "move_axis", "params": {"axis": 1, "point_mm": -270, "mm_per_second": 100, "relative":true}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {"machine_func": "move_axis", "params": {"axis": 0, "point_mm": -216.5, "mm_per_second": 100, "relative":true}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {"machine_func": "set_position", "params": {"axis": 1, "position_mm": 0}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {"machine_func": "set_position", "params": {"axis": 0, "position_mm": 0}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {"machine_func": "wait_for_heaters_at_target", "params": {"timeout_minutes":5,"check":[true, false]}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {"axes": "z"}, "jsonrpc": "2.0", "method": "home"}')
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "execute_queue"}')
        return "Home Z process", None 
    except Exception as e:
        return "There is something wrong with my HOME-Z commands.", str(e)
        
def park():
    try:
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "close_queue"}')
        rpc_call_raw('{"params": {"clear": true}, "jsonrpc": "2.0", "method": "open_queue"}')
        rpc_call_raw('{"params": {"machine_func": "move", "params": {"point_mm":[0, 0, 50, 0], "mm_per_second":3.0, "relative":[true, true, false, true]}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {"machine_func": "move", "params": {"point_mm":[0, 130, 0, 0], "mm_per_second":100.0, "relative":[true, false, true, true]}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {"machine_func": "move", "params": {"point_mm":[147.5, 0, 0, 0], "mm_per_second":100.0, "relative":[false, true, true, true]}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {"machine_func": "move", "params": {"point_mm":[145.5, 0, 0, 0], "mm_per_second":100.0, "relative":[false, true, true, true]}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {"machine_func": "move", "params": {"point_mm":[0, 175, 0, 0], "mm_per_second":100.0, "relative":[true, false, true, true]}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "execute_queue"}')
        return "Park process", None 
    except Exception as e:
        return "There is something wrong with my PARK commands.", str(e)

def move_home_z():
    try:
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "close_queue"}')
        rpc_call_raw('{"params": {"clear": true}, "jsonrpc": "2.0", "method": "open_queue"}')
        rpc_call_raw('{"params": {"machine_func": "move", "params": {"point_mm":[0,0,0,0], "mm_per_second":100.0, "relative":[false, false, true, true]}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {"machine_func": "move", "params": {"point_mm":[0,0,0,0], "mm_per_second":3.0, "relative":[true, true, false, true]}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "execute_queue"}')
        return "Move process", None 
    except Exception as e:
        return "There is something wrong with my MOVE commands.", str(e)
        
def move_z_up_001():
    try:
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "close_queue"}')
        rpc_call_raw('{"params": {"clear": true}, "jsonrpc": "2.0", "method": "open_queue"}')
        rpc_call_raw('{"params": {"machine_func": "move", "params": {"point_mm":[0,0,-0.01,0], "mm_per_second":1.0, "relative":[true, true, true, true]}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "execute_queue"}')
        return "Move process", None 
    except Exception as e:
        return "There is something wrong with my MOVE commands.", str(e)

def move_z_up_01():
    try:
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "close_queue"}')
        rpc_call_raw('{"params": {"clear": true}, "jsonrpc": "2.0", "method": "open_queue"}')
        rpc_call_raw('{"params": {"machine_func": "move", "params": {"point_mm":[0,0,-0.1,0], "mm_per_second":1.0, "relative":[true, true, true, true]}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "execute_queue"}')
        return "Move process", None 
    except Exception as e:
        return "There is something wrong with my MOVE commands.", str(e)

def move_z_up_1():
    try:
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "close_queue"}')
        rpc_call_raw('{"params": {"clear": true}, "jsonrpc": "2.0", "method": "open_queue"}')
        rpc_call_raw('{"params": {"machine_func": "move", "params": {"point_mm":[0,0,-1,0], "mm_per_second":2.0, "relative":[true, true, true, true]}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "execute_queue"}')
        return "Move process", None 
    except Exception as e:
        return "There is something wrong with my MOVE commands.", str(e)

def move_z_up_10():
    try:
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "close_queue"}')
        rpc_call_raw('{"params": {"clear": true}, "jsonrpc": "2.0", "method": "open_queue"}')
        rpc_call_raw('{"params": {"machine_func": "move", "params": {"point_mm":[0,0,-10,0], "mm_per_second":3.0, "relative":[true, true, true, true]}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "execute_queue"}')
        return "Move process", None 
    except Exception as e:
        return "There is something wrong with my MOVE commands.", str(e)

def move_z_up_100():
    try:
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "close_queue"}')
        rpc_call_raw('{"params": {"clear": true}, "jsonrpc": "2.0", "method": "open_queue"}')
        rpc_call_raw('{"params": {"machine_func": "move", "params": {"point_mm":[0,0,-100,0], "mm_per_second":3.0, "relative":[true, true, true, true]}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "execute_queue"}')
        return "Move process", None 
    except Exception as e:
        return "There is something wrong with my MOVE commands.", str(e)

def move_z_down_001():
    try:
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "close_queue"}')
        rpc_call_raw('{"params": {"clear": true}, "jsonrpc": "2.0", "method": "open_queue"}')
        rpc_call_raw('{"params": {"machine_func": "move", "params": {"point_mm":[0,0,0.01,0], "mm_per_second":1.0, "relative":[true, true, true, true]}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "execute_queue"}')
        return "Move process", None 
    except Exception as e:
        return "There is something wrong with my MOVE commands.", str(e)

def move_z_down_01():
    try:
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "close_queue"}')
        rpc_call_raw('{"params": {"clear": true}, "jsonrpc": "2.0", "method": "open_queue"}')
        rpc_call_raw('{"params": {"machine_func": "move", "params": {"point_mm":[0,0,0.1,0], "mm_per_second":1.0, "relative":[true, true, true, true]}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "execute_queue"}')
        return "Move process", None 
    except Exception as e:
        return "There is something wrong with my MOVE commands.", str(e)

def move_z_down_1():
    try:
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "close_queue"}')
        rpc_call_raw('{"params": {"clear": true}, "jsonrpc": "2.0", "method": "open_queue"}')
        rpc_call_raw('{"params": {"machine_func": "move", "params": {"point_mm":[0,0,1,0], "mm_per_second":2.0, "relative":[true, true, true, true]}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "execute_queue"}')
        return "Move process", None 
    except Exception as e:
        return "There is something wrong with my MOVE commands.", str(e)

def move_z_down_10():
    try:
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "close_queue"}')
        rpc_call_raw('{"params": {"clear": true}, "jsonrpc": "2.0", "method": "open_queue"}')
        rpc_call_raw('{"params": {"machine_func": "move", "params": {"point_mm":[0,0,10,0], "mm_per_second":3.0, "relative":[true, true, true, true]}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "execute_queue"}')
        return "Move process", None 
    except Exception as e:
        return "There is something wrong with my MOVE commands.", str(e)

def move_z_down_100():
    try:
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "close_queue"}')
        rpc_call_raw('{"params": {"clear": true}, "jsonrpc": "2.0", "method": "open_queue"}')
        rpc_call_raw('{"params": {"machine_func": "move", "params": {"point_mm":[0,0,100,0], "mm_per_second":3.0, "relative":[true, true, true, true]}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "execute_queue"}')
        return "Move process", None 
    except Exception as e:
        return "There is something wrong with my MOVE commands.", str(e)

def z_zero():
    try:
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "close_queue"}')
        rpc_call_raw('{"params": {"clear": true}, "jsonrpc": "2.0", "method": "open_queue"}')
        rpc_call_raw('{"params": {"machine_func": "set_position", "params": {"axis": 2, "position_mm": 0}}, "jsonrpc": "2.0", "method": "machine_query_command"}')
        rpc_call_raw('{"params": {}, "jsonrpc": "2.0", "method": "execute_queue"}')
        return "Move process", None 
    except Exception as e:
        return "There is something wrong with my MOVE commands.", str(e)

def action_menu_0():
    try:
        user_file = input(f"\nEnter the filename to upload and print (or press Enter for default '{LOCAL_FILE_PATH}'): ").strip()
        if not user_file:
            user_file = LOCAL_FILE_PATH
        return rpc_file_upload(user_file)
    except EOFError:
        return None, "Upload cancelled."
    
def action_menu_1():
    return rpc_call_raw(JSON_menu_1)
    
def action_menu_2():
    return rpc_call_raw(JSON_menu_2)    
    
def action_menu_3():
    return rpc_call_raw(JSON_menu_3)

def action_menu_4():
    return rpc_call_raw(JSON_menu_4)

def action_menu_5():
    return rpc_call_raw(JSON_menu_5)
    
def action_menu_6():
    return rpc_call_raw(JSON_menu_6)

def action_menu_7():
    return park()
    
def action_menu_8():
    return rpc_call_raw(JSON_menu_8)
    
def action_menu_9():
    return rpc_call_raw(JSON_menu_9) 

def action_menu_A():
    return home_z()

def action_menu_B():
    return home_xy() 

def action_menu_C():
    return move_home_z()

def action_menu_D():
    return z_zero() 
    
def action_menu_E():
    return move_z_up_001()
    
def action_menu_F():
    return move_z_up_01()

def action_menu_G():
    return move_z_up_1()
    
def action_menu_H():
    return move_z_up_10()
    
def action_menu_I():
    return  move_z_up_100()

def action_menu_J():
    return  move_z_down_001()

def action_menu_K():
    return  move_z_down_01()

def action_menu_L():
    return  move_z_down_1()
    
def action_menu_M():
    return  move_z_down_10()
    
def action_menu_N():
    return  move_z_down_100()

def action_menu_P():
    return print_rpc()

def scan_lan_for_printers_zeroconf() -> List[str]:
    if not HAS_ZEROCONF:
        return []
    found_printers = []
    
    def on_service_state_change(zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Added:
            info = zeroconf.get_service_info(service_type, name)
            if info and info.parsed_addresses():
                found_printers.append(info.parsed_addresses()[0])

    zc = Zeroconf()
    browser = ServiceBrowser(zc, "_makerbot-jsonrpc._tcp.local.", handlers=[on_service_state_change])
    time.sleep(3)
    zc.close()
    return sorted(list(set(found_printers)))

def action_menu_S():
    clear_screen()
    print("Starting TCP Port 12309 scan... This may take a few seconds.")
    found = scan_lan_for_printers(12309)
    print("\nScan complete.")
    if found:
        print("Found printers at:")
        for ip in found:
            print(f"  - {ip}")
    else:
        print("No printers found.")
    try:
        input("\nPress Enter to return to monitor...")
    except EOFError:
        pass
    return None, None

def action_menu_Z():
    if not HAS_ZEROCONF:
        return None, f"Zeroconf library not installed in the current environment. Run '{sys.executable} -m pip install zeroconf' to install it."
    clear_screen()
    print("Starting Zeroconf/mDNS scan... Listening for 3 seconds.")
    found = scan_lan_for_printers_zeroconf()
    print("\nScan complete.")
    if found:
        print("Found printers at:")
        for ip in found:
            print(f"  - {ip}")
    else:
        print("No printers found.")
    try:
        input("\nPress Enter to return to monitor...")
    except EOFError:
        pass
    return None, None

def action_menu_x():
    return rpc_call_raw(JSON_menu_x)  

def action_menu_enter():
    return rpc_call_raw(JSON_menu_enter_3)
    
def action_menu_space():
    global pause
    pause = not pause
    if pause == True:
        return rpc_call_raw(JSON_menu_space_1) 
    else:
        return rpc_call_raw(JSON_menu_space_2)   
    
def clear_screen():
    try:
        #print()
        os.system('cls' if os.name == 'nt' else 'clear')
    except Exception:
        pass 

def display_monitor(status: Dict[str, Any], feedback: str):
    global custom_code
    clear_screen()
    
    GREEN_CIRCLE = "\033[92m●\033[0m"  
    RED_CIRCLE = "\033[91m●\033[0m"  
    heating_icon = GREEN_CIRCLE if status["preheating"] else RED_CIRCLE

    print("=" * 50)
    print(f"| MAKERBOT CONTROLLER AND MONITOR | {PRINTER_IP}")
    print("=" * 50)
    print(f"File name: {status['filename']}")
    print(f"Process: {status['process']}")
    print(f"Step: {status['step']}")
    print(f"Progress: {status['progress']}")
    print(f"Elapsed time: {status['elapsed_time']}")
    print("-" * 50)
    print(f"HEAT: {heating_icon}")
    print(f"Extruder: {status['extruder_current']} / {status['extruder_target']} °C")
    print(f"Chamber: {status['chamber_current']} / {status['chamber_target']} °C")
    print("-"*22, "MENU", "-"*22)
    print(f" 0 - Upload And Print")
    print(f" 1 - Print Again")
    print(f" 2 - Preheat to 180 °C")
    print(f" 3 - Load Filament")
    print(f" 4 - Unload Filament")
    print(f" 5 - Cool")
    print(f" 6 - Lower Build Plate")
    print(f" 7 - Park")
    print(f" 8 - Heat Up To 280 °C - Change the nozzle")
    print(f" 9 - Attach Smart Extruder")
    print("-" * 50)
    print(f" A - Home Z")
    print(f" B - Home X/Y")
    print(f" C - Move to X=0mm/Y=0mm/Z=0mm")
    print(f" D - Zero Z")
    print("-" * 50)
    print(f" E/F/G/H/I - Move to Z UP 0.01mm/0.1mm/1.0mm/10mm/100mm")
    print(f" J/K/L/M/N - Move to Z DOWN 0.01mm/0.1mm/1.0mm/10mm/100mm")
    print("-" * 50)
    print(f" S - Scan LAN for Printers (TCP Port 12309)")
    print(f" Z - Scan LAN for Printers (Zeroconf / mDNS)")
    print("-" * 50)
    print(f" ENTER  - OK - print ready")
    print(f" SPACE  - Pause / Resume")
    print(f" CTRL+x - Cancel")
    print("-" * 50)
    print(f" ESC - Exit")
    print("-" * 50)

    if feedback:
      
        if "ERROR" in feedback:
            print(f"\n<<< ❌ LAST ACTION ERROR >>>")
            print(f"\033[91m{feedback}\033[0m")
        elif "WARNING" in feedback or "INFO" in feedback:
            print(f"\n<<< ⓘ LAST OPERATION FEEDBACK >>>")
            print(f"\033[93m{feedback}\033[0m")
        else:
            print(f"\n<<< ✅ LAST ACTION SUCCESS >>>")
            print(f"\033[92m{feedback}\033[0m")
        print("<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<\n")
    # -------------------------------
    pass

def check_port(ip: str, port: int, timeout: float = 0.1) -> Optional[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((ip, port))
            return ip
    except Exception:
        return None

def scan_lan_for_printers(port: int) -> List[str]:
    print(f"Scanning the local network for MakerBot printers on port {port} (this takes a few seconds)...")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    
    if IP == '127.0.0.1':
        return []
    
    prefix = '.'.join(IP.split('.')[:-1]) + '.'
    ips_to_scan = [prefix + str(i) for i in range(1, 255)]
    found = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=100) as executor:
        future_to_ip = {executor.submit(check_port, ip, port): ip for ip in ips_to_scan}
        for future in concurrent.futures.as_completed(future_to_ip):
            res = future.result()
            if res:
                found.append(res)
    return sorted(found)

def get_config():
    config = configparser.ConfigParser()
    cfg_filename = 'makerbot.cfg'
    cfg_filename = os.path.join(os.path.dirname(os.path.abspath(__file__)), cfg_filename)
    global FIRSTRUN

    if not os.path.exists(cfg_filename):
        FIRSTRUN=True
        print()
        print(f"The {cfg_filename} not found.")
        print("Please enter the following settings:")
        print()
        
        found_printers = scan_lan_for_printers(PRINTER_PORT_SECURE)
        if found_printers:
            print("\nFound MakerBot printers at the following IP addresses:")
            for idx, p_ip in enumerate(found_printers):
                print(f"  {idx + 1}. {p_ip}")
            ip = input(f"\n3D Printer IP address (or press Enter to use {found_printers[0]}): ").strip()
            if not ip:
                ip = found_printers[0]
        else:
            print("\nNo MakerBot printers found on the local network automatically.")
            ip = input("3D Printer IP address: ").strip()

        user = input("Username: ").strip()
        code = LOCAL_CODE

        config['SETTINGS'] = {
            'PRINTER_IP': ip,
            'USERNAME': user,
            'LOCAL_CODE': code
        }

        with open(cfg_filename, 'w', encoding='utf-8') as configfile:
            config.write(configfile)
        print(f"Configuration saved: {cfg_filename}\n")
    else:
        config.read(cfg_filename)
    return config['SETTINGS']

def update_config_code(new_code, cfg_filename='makerbot.cfg'):
    cfg_filename = os.path.join(os.path.dirname(os.path.abspath(__file__)), cfg_filename)
    config = configparser.ConfigParser()
    config.read(cfg_filename)
    
    if 'SETTINGS' not in config:
        config.add_section('SETTINGS')
    
    config.set('SETTINGS', 'LOCAL_CODE', new_code)
    
    with open(cfg_filename, 'w', encoding='utf-8') as configfile:
        config.write(configfile)
    print("Local code successfully updated in config file.")

def create_init_ssl_socket(ip, port):
    raw_init_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw_init_socket.settimeout(120)
    raw_init_socket.connect((ip, port))
 
    ssl_init_context = ssl.create_default_context()
    ssl_init_context.check_hostname = False
    ssl_init_context.verify_mode = ssl.CERT_NONE
    
    s = ssl_init_context.wrap_socket(raw_init_socket, server_hostname=ip)
    
    print(f"✅ **SSL/TLS connection is ready (Port: {port}).**")
    return s

def _process_response_chunk(chunk: str, expected_id: int, is_initial: bool = False) -> Tuple[Optional[Dict[str, Any]], Optional[str], bool]:
    result = None
    error = None
    for line in chunk.split('\n'):
        if line.strip():
            try:
                json_response = json.loads(line)
                response_id = json_response.get('id')
                
                if response_id == expected_id:
                    if 'error' in json_response:
                        error = json_response['error'].get('message', 'Unknown error!')
                    else:
                        result = json_response.get('result')
                    return result, error, True
                    
            except json.JSONDecodeError:
                # This is not JSON.
                pass
    return result, error, False 
    
def perform_stable_auth(ssl_socket: socket.socket, request_id: int) -> Tuple[Optional[str], int]:
    global USERNAME, LOCAL_CODE
    if not ssl_socket:
        return None, "The socket is not initialized."
    rpc_data = json.loads('{"params": {"username":"' + USERNAME + '"}, "jsonrpc": "2.0", "method": "handshake", "id": 1}')
    final_payload = json.dumps(rpc_data)
    ssl_socket.sendall(final_payload.encode('utf-8'))
    request_id += 1
    chunk = ssl_socket.recv(4096).decode('utf-8')
    if not chunk:
        return None, "Connection closed before initial response."
    result, error, found = _process_response_chunk(chunk, request_id, is_initial=True)
        
    if found:
        return result, error
    
    if FIRSTRUN == True:
        print("Push the button on the printer!")
        rpc_data = json.loads('{"params": {"username":"' + USERNAME + '", "local_secret":""}, "jsonrpc": "2.0", "method": "authorize", "id": 2}')
        final_payload = json.dumps(rpc_data)
        ssl_socket.sendall(final_payload.encode('utf-8'))
        request_id += 1
        chunk = ssl_socket.recv(4096).decode('utf-8')
        try:
            response_data = json.loads(chunk)
            if "result" in response_data:
                result_content = response_data["result"]
                if "local_code" in result_content:
                    new_code = result_content["local_code"]
                    print(f"Authentication successful!")
                    update_config_code(new_code)
                else:
                    print("Error: The data received is not valid DATA.")
            else:
                print("Error: The data received is not valid RESULT.")
        except json.JSONDecodeError:
            print("Error: The data received is not valid JSON.")
        time.sleep(1)
    else:    
        rpc_data = json.loads('{"params": {"username":"' + USERNAME + '", "local_secret":"", "local_code": "' + LOCAL_CODE + '"}, "jsonrpc": "2.0", "method": "reauthorize", "id": 2}')
        final_payload = json.dumps(rpc_data)
        ssl_socket.sendall(final_payload.encode('utf-8'))
        request_id += 1
        chunk = ssl_socket.recv(4096).decode('utf-8')
        try:
            response_data = json.loads(chunk)
            if "error" in response_data:
                print(f"Authentication failed. Please delete the makerbot.cfg file and restart the program.")
                is_running.clear()
                input()
                sys.exit(1)
            else:
                print(f"Authentication successful!")
                time.sleep(1)
        except json.JSONDecodeError:
            print("Error: The data received is not valid JSON.")
            
    if not chunk:
        return None, "Connection closed before initial response."
    result, error, found = _process_response_chunk(chunk, request_id, is_initial=True)
        
    if found:
        return result, error      
    return request_id

# ==============================================================================
#                 MAIN PROGRAM LOGIC
# ==============================================================================

def main():
    global ssl_socket, global_request, access_token, last_action_feedback, last_feedback_time, PRINTER_IP, USERNAME, LOCAL_CODE
    print(f"[{time.strftime('%H:%M:%S')}] The MakerBot Remote Control program is starting (Monitor Mode).")
    
    settings = get_config()

    PRINTER_IP = settings.get('PRINTER_IP')
    USERNAME = settings.get('USERNAME')
    LOCAL_CODE = settings.get('LOCAL_CODE')

    
    # 1. ESTABLISHING AN SSL/TLS CONNECTION
    try:
        ssl_socket = create_init_ssl_socket(PRINTER_IP, PRINTER_PORT_SECURE)
    except Exception as e:
        print(f"\n❌ **ERROR establishing connection: {e}. Check IP address.")
        return

    # 2. PERFORM AUTHENTICATION
    next_id = perform_stable_auth(ssl_socket, global_request)
    global_request = next_id 
    
    # 3. STARTING A LISTENER THREAD
    is_running.set()
    listener = ListenerThread(ssl_socket)
    listener.start()
    
    # 4. MAIN MONITOR CYCLE AND INPUT MANAGEMENT
    timeout_seconds = 1.0     
    
    # Actions related to the menu items
    menu_actions = {
        '0': action_menu_0,
        '1': action_menu_1,
        '2': action_menu_2,
        '3': action_menu_3,
        '4': action_menu_4,
        '5': action_menu_5,
        '6': action_menu_6,
        '7': action_menu_7,
        '8': action_menu_8,
        '9': action_menu_9,
        
        'A': action_menu_A,
        'B': action_menu_B,
        'C': action_menu_C,
        'D': action_menu_D,
        
        'E': action_menu_E,
        'F': action_menu_F,
        'G': action_menu_G,
        'H': action_menu_H,
        'I': action_menu_I,
        'J': action_menu_J,
        'K': action_menu_K,
        'L': action_menu_L,
        'M': action_menu_M,
        'N': action_menu_N,
        
        'S': action_menu_S,
        's': action_menu_S,
        'Z': action_menu_Z,
        'z': action_menu_Z,

        '\x18': action_menu_x,
        '\r': action_menu_enter,
        '\n': action_menu_enter,
        ' ': action_menu_space,
    }
    
    try:
        while is_running.is_set():
            start_time = time.time()
            with status_lock:
                status_copy = printer_status.copy()
                feedback_copy = last_action_feedback
                if last_action_feedback and (time.time() - last_feedback_time) > FEEDBACK_DURATION:
                    last_action_feedback = ""
                    last_feedback_time = 0.0
                    feedback_copy = ""
                    
            display_monitor(status_copy, feedback_copy)
            
            user_input = None
            
            if IS_WINDOWS:
                if msvcrt.kbhit():
                    char = msvcrt.getch()
                    try:
                        user_input = char.decode('utf-8')
                        if len(user_input) > 1: user_input = None 
                    except UnicodeDecodeError:
                        user_input = None

                time_to_wait = timeout_seconds - (time.time() - start_time)
                if time_to_wait > 0:
                    time.sleep(time_to_wait)
                        
            else:
                if sys.stdin.isatty():
                    fd = sys.stdin.fileno()
                    old_settings = termios.tcgetattr(fd)
                    try:
                        tty.setcbreak(fd)
                        i, o, e = select.select([sys.stdin], [], [], timeout_seconds)
                        if i:
                            user_input = sys.stdin.read(1)
                            if user_input == '':
                                is_running.clear()
                                break
                    except select.error:
                        continue 
                    except ValueError:
                        is_running.clear()
                        break
                    finally:
                        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                else:
                    try:
                        i, o, e = select.select([sys.stdin], [], [], timeout_seconds)
                    except select.error:
                        continue 
                    except ValueError:
                        is_running.clear()
                        break
                        
                    if i:
                        try:
                            user_input = sys.stdin.readline().strip()
                        except EOFError:
                            is_running.clear()
                            break

            if user_input:
                if user_input == '\x1b':
                    print("\nTo request to exit. Close connection...")
                    is_running.clear()
                    break
                
                action_sleep_time = 0.5
                new_feedback = ""
                
                if user_input in menu_actions:
                    method_name, error_message = menu_actions[user_input]()
                    
                    if error_message:
                        if user_input.upper() in ['S', 'Z']:
                            new_feedback = f"ERROR: {error_message}"
                        else:
                            new_feedback = f"ERROR: RPC send failed. {error_message}"
                        action_sleep_time = 2.0
                    elif method_name:
                        new_feedback = f"SUCCESS: The following command was sent to the printer: ({method_name})"

                    with status_lock:
                        last_action_feedback = new_feedback
                        last_feedback_time = time.time()
                        
                    time.sleep(action_sleep_time) 

                else:
                    with status_lock:
                        last_action_feedback = f"WARNING: Invalid selection: {user_input}."
                        last_feedback_time = time.time()
                    time.sleep(0.5)
                
            
    except KeyboardInterrupt:
        print("\n\nTo exit (Ctrl+C). Close connection...")
        is_running.clear()

    print("Waiting for closing Listener.")
    if listener.is_alive():
        listener.join(2) 
    
    if ssl_socket:
        ssl_socket.close()
        print("Connection is closed. Bye!")


if __name__ == '__main__':

    main()

