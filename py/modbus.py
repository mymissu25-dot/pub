import threading
import time
import customtkinter as ctk
import serial
import serial.tools.list_ports

# 글로벌 변수 설정
ser = None

# 응답 프레임을 받을 때, 이 시간(초) 이상 새 데이터가 안 들어오면 응답이 끝난 것으로 간주합니다.
FRAME_GAP_SEC = 0.05

# 지원할 모드버스 읽기 기능 코드
FUNCTION_CODES = {
    "01: 코일 읽기 (Read Coils)": 0x01,
    "02: 디스크리트 입력 읽기 (Read Discrete Inputs)": 0x02,
    "03: 홀딩 레지스터 읽기 (Read Holding Registers)": 0x03,
    "04: 입력 레지스터 읽기 (Read Input Registers)": 0x04,
}

MODBUS_EXCEPTIONS = {
    1: "잘못된 기능 코드 (Illegal Function)",
    2: "잘못된 데이터 주소 (Illegal Data Address)",
    3: "잘못된 데이터 값 (Illegal Data Value)",
    4: "슬레이브 디바이스 오류 (Slave Device Failure)",
}

# --- GUI 화면 설정 ---
ctk.set_appearance_mode("System")
window = ctk.CTk()
window.title("모드버스 RTU 마스터 (읽기)")
window.geometry("640x780")

# --- 모드버스 관련 함수 ---

# 모드버스 CRC16 계산
def modbus_crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x0001:
                crc >>= 1
                crc ^= 0xA001
            else:
                crc >>= 1
    return crc.to_bytes(2, byteorder="little")

# 읽기 요청 프레임 생성 (슬레이브ID + 기능코드 + 시작주소(2byte) + 개수(2byte) + CRC(2byte))
def build_read_request(slave_id, func_code, start_addr, quantity):
    frame = bytes([slave_id, func_code])
    frame += start_addr.to_bytes(2, "big")
    frame += quantity.to_bytes(2, "big")
    frame += modbus_crc16(frame)
    return frame

# 수신된 응답 프레임의 CRC 검증
def check_crc(frame: bytes) -> bool:
    if len(frame) < 3:
        return False
    body, crc_recv = frame[:-2], frame[-2:]
    return modbus_crc16(body) == crc_recv

# 응답 프레임 파싱 (성공 시 (값 리스트, None), 실패 시 (None, 에러메시지))
def parse_response(func_code, resp: bytes, quantity):
    if len(resp) < 5:
        return None, "응답 길이가 너무 짧습니다."

    if not check_crc(resp):
        return None, "CRC 오류 (응답이 손상되었을 수 있습니다)."

    fc = resp[1]
    if fc & 0x80:  # 예외 응답 (기능코드의 최상위 비트가 1)
        exc_code = resp[2]
        msg = MODBUS_EXCEPTIONS.get(exc_code, f"알 수 없는 예외 코드 {exc_code}")
        return None, f"모드버스 예외 응답: {msg}"

    byte_count = resp[2]
    data = resp[3:3 + byte_count]

    if func_code in (0x01, 0x02):  # 코일 / 디스크리트 입력: 비트 단위
        bits = []
        for i in range(quantity):
            byte_idx = i // 8
            bit_idx = i % 8
            if byte_idx < len(data):
                bit = (data[byte_idx] >> bit_idx) & 0x01
            else:
                bit = 0
            bits.append(bit)
        return bits, None

    elif func_code in (0x03, 0x04):  # 홀딩 / 입력 레지스터: 16bit 워드 단위
        values = []
        for i in range(0, byte_count - 1, 2):
            val = (data[i] << 8) | data[i + 1]
            values.append(val)
        return values, None

    return None, "지원하지 않는 기능 코드입니다."

# --- 로그/결과 출력 함수 ---

def log_message(message):
    log_area.configure(state="normal")
    log_area.insert("end", message + "\n")
    log_area.see("end")
    log_area.configure(state="disabled")

def show_result(func_code, start_addr, values):
    result_area.configure(state="normal")
    result_area.delete("1.0", "end")

    # 세로로 한 줄에 하나씩, 번호/값 칸을 나눠 표(테이블) 형태로 표시
    col1_width = 6   # 번호 칸
    col2_width = 10  # 값 칸

    header = f"{'번호':>{col1_width}} | {'값':<{col2_width}}\n"
    separator = ("-" * col1_width) + "-+-" + ("-" * col2_width) + "\n"
    result_area.insert("end", header)
    result_area.insert("end", separator)

    for i, val in enumerate(values):
        num = start_addr + i
        if func_code in (0x01, 0x02):
            val_str = "ON" if val else "OFF"
        else:
            val_str = str(val)
        line = f"{num:>{col1_width}} | {val_str:<{col2_width}}\n"
        result_area.insert("end", line)

    result_area.configure(state="disabled")

def clear_result_with_message(msg):
    result_area.configure(state="normal")
    result_area.delete("1.0", "end")
    result_area.insert("end", msg)
    result_area.configure(state="disabled")

# --- 포트 갱신 ---

def refresh_ports():
    ports = [p.device for p in serial.tools.list_ports.comports()]
    if not ports:
        ports = ["연결된 포트 없음"]
    port_combo.configure(values=ports)
    port_combo.set(ports[0])

# --- 읽기 요청 실행 (버튼 클릭 시 스레드로 실행) ---

def start_read():
    t = threading.Thread(target=_do_read, daemon=True)
    t.start()

def _do_read():
    global ser

    target_port = port_combo.get()
    if target_port == "연결된 포트 없음":
        log_message("❌ 연결할 UART 장치를 확인하세요.")
        return

    try:
        baud = int(baud_combo.get())
        slave_id = int(slave_entry.get())
        func_label = func_combo.get()
        func_code = FUNCTION_CODES[func_label]
        start_addr = int(addr_entry.get())
        quantity = int(qty_entry.get())
    except ValueError:
        log_message("❌ 슬레이브ID / 시작주소 / 개수는 숫자로 입력하세요.")
        return

    if quantity <= 0:
        log_message("❌ 읽을 개수는 1 이상이어야 합니다.")
        return

    request = build_read_request(slave_id, func_code, start_addr, quantity)

    try:
        ser = serial.Serial(target_port, baud, timeout=0.05)
        ser.write(request)
        log_message(f"[송신] {request.hex(' ').upper()}")

        # 프레임 갭(간격) 기반으로 응답 수신 (특정 바이트로 자르지 않음)
        buffer = bytearray()
        deadline = time.time() + 1.5  # 전체 응답 대기 타임아웃
        last_recv_time = time.time()

        while time.time() < deadline:
            if ser.in_waiting > 0:
                buffer += ser.read(ser.in_waiting)
                last_recv_time = time.time()
            elif buffer and (time.time() - last_recv_time) >= FRAME_GAP_SEC:
                break
            else:
                time.sleep(0.005)

        ser.close()

        if not buffer:
            log_message("❌ 응답 없음 (타임아웃). 배선/슬레이브ID/통신속도를 확인하세요.")
            clear_result_with_message("응답 없음")
            return

        log_message(f"[수신] {bytes(buffer).hex(' ').upper()}")

        values, err = parse_response(func_code, bytes(buffer), quantity)
        if err:
            log_message(f"❌ {err}")
            clear_result_with_message(err)
        else:
            show_result(func_code, start_addr, values)

    except Exception as e:
        log_message(f"❌ 오류 발생: {str(e)}")
    finally:
        if ser and ser.is_open:
            ser.close()

def on_closing():
    global ser
    if ser and ser.is_open:
        ser.close()
    window.destroy()

window.protocol("WM_DELETE_WINDOW", on_closing)

# --- UI 레이아웃 배치 ---

title_label = ctk.CTkLabel(window, text="Modbus RTU Master", font=("맑은 고딕", 24, "bold"))
title_label.pack(pady=15)

# 포트/속도 설정
port_frame = ctk.CTkFrame(window, fg_color="transparent")
port_frame.pack(pady=5)

ctk.CTkLabel(port_frame, text="포트:").grid(row=0, column=0, padx=5)
port_combo = ctk.CTkComboBox(port_frame, width=120)
port_combo.grid(row=0, column=1, padx=5)

ctk.CTkLabel(port_frame, text="속도:").grid(row=0, column=2, padx=5)
baud_combo = ctk.CTkComboBox(port_frame, values=["9600", "19200", "38400", "57600", "115200"], width=100)
baud_combo.set("9600")
baud_combo.grid(row=0, column=3, padx=5)

btn_refresh = ctk.CTkButton(port_frame, text="🔄 갱신", width=60, command=refresh_ports)
btn_refresh.grid(row=0, column=4, padx=5)

# 모드버스 요청 설정
modbus_frame = ctk.CTkFrame(window, fg_color="transparent")
modbus_frame.pack(pady=10)

ctk.CTkLabel(modbus_frame, text="슬레이브 ID:").grid(row=0, column=0, padx=5, pady=5, sticky="e")
slave_entry = ctk.CTkEntry(modbus_frame, width=60)
slave_entry.insert(0, "1")
slave_entry.grid(row=0, column=1, padx=5, pady=5, sticky="w")

ctk.CTkLabel(modbus_frame, text="기능:").grid(row=0, column=2, padx=5, pady=5, sticky="e")
func_combo = ctk.CTkComboBox(modbus_frame, values=list(FUNCTION_CODES.keys()), width=280)
func_combo.set("03: 홀딩 레지스터 읽기 (Read Holding Registers)")
func_combo.grid(row=0, column=3, padx=5, pady=5, sticky="w")

ctk.CTkLabel(modbus_frame, text="시작 주소:").grid(row=1, column=0, padx=5, pady=5, sticky="e")
addr_entry = ctk.CTkEntry(modbus_frame, width=80)
addr_entry.insert(0, "0")
addr_entry.grid(row=1, column=1, padx=5, pady=5, sticky="w")

ctk.CTkLabel(modbus_frame, text="개수:").grid(row=1, column=2, padx=5, pady=5, sticky="e")
qty_entry = ctk.CTkEntry(modbus_frame, width=80)
qty_entry.insert(0, "16")
qty_entry.grid(row=1, column=3, padx=5, pady=5, sticky="w")

btn_read = ctk.CTkButton(window, text="📡 읽기 요청", fg_color="green", hover_color="darkgreen",
                          width=150, command=start_read)
btn_read.pack(pady=10)

# 결과 표시 영역 (화면에 최대한 많이 보이도록 넓게 + 촘촘한 폰트)
ctk.CTkLabel(window, text="결과", font=("맑은 고딕", 14, "bold")).pack(pady=(10, 0))
result_area = ctk.CTkTextbox(window, width=600, height=320, font=("Consolas", 12))
result_area.pack(pady=5, padx=10, fill="both", expand=True)
result_area.configure(state="disabled")

# 통신 로그(송/수신 프레임) 표시 영역
# wrap="none": 수신 프레임(HEX)이 길어도 자동으로 줄바꿈되지 않고 한 줄로 유지되며,
# 필요하면 가로 스크롤로 확인합니다.
ctk.CTkLabel(window, text="통신 로그 (HEX)", font=("맑은 고딕", 14, "bold")).pack(pady=(10, 0))
log_area = ctk.CTkTextbox(window, width=600, height=140, font=("Consolas", 12), wrap="none")
log_area.pack(pady=5, padx=10, fill="x")
log_area.configure(state="disabled")

# 실행 시 사용 가능한 포트 자동 검색
refresh_ports()

window.mainloop()