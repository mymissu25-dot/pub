import threading
import time
import customtkinter as ctk
import serial
import serial.tools.list_ports

# 글로벌 변수 설정
ser = None
continuous_running = False
continuous_thread = None
error_count = 0

# 응답 프레임을 받을 때, 이 시간(초) 이상 새 데이터가 안 들어오면 응답이 끝난 것으로 간주합니다.
FRAME_GAP_SEC = 0.05

# 계속읽기 모드 설정
CONTINUOUS_INTERVAL_SEC = 0.2   # 요청 주기
CONTINUOUS_RESPONSE_TIMEOUT = 1.0  # 요청당 최대 응답 대기 시간

# 결과창에서 세로로 몇 줄 채우면 오른쪽에 새 칸을 시작할지 (낮은 해상도 화면 기준 10줄)
RESULT_ROWS_PER_COLUMN = 10

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
window.geometry("700x640")  # 730 → 640
window.minsize(680, 680)

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

# --- 로그/결과 출력 함수 ---
# 아래 함수들은 계속읽기 스레드 등 백그라운드 스레드에서도 호출되므로,
# 실제 위젯 변경은 window.after(0, ...)로 메인 스레드(Tk 이벤트 루프)에 맡깁니다.
# 이렇게 하면 "왼쪽으로 튀었다가 복원되는" 중간 상태가 화면에 그려질 틈 없이
# 한 번에(원자적으로) 반영됩니다.

def log_message(message):
    def _apply():
        log_area.configure(state="normal")
        log_area.insert("end", message + "\n")
        # see() 대신 세로 스크롤만 맨 아래로 이동시켜서 가로 스크롤 위치는 아예 건드리지 않습니다.
        log_area.yview_moveto(1.0)
        log_area.configure(state="disabled")
    window.after(0, _apply)

def show_result(func_code, start_addr, values):
    def _apply():
        # 결과창은 매번 전체를 지우고 다시 그리기 때문에, 그리기 전 가로 스크롤 위치를
        # 기억했다가 그린 직후 같은 이벤트 루프 처리 안에서 바로 복원합니다.
        x_frac = result_area.xview()[0]

        result_area.configure(state="normal")
        result_area.delete("1.0", "end")

        # 세로로 채우다가 RESULT_ROWS_PER_COLUMN줄이 넘으면 오른쪽에 새 칸(열)을 만들어 이어서 표시
        col1_width = 6   # 번호 칸
        col2_width = 10  # 값 칸
        col_gap = "   "

        num_items = len(values)
        num_cols = max(1, -(-num_items // RESULT_ROWS_PER_COLUMN))  # 올림 나눗셈

        header_cell = f"{'번호':>{col1_width}} | {'값':<{col2_width}}"
        sep_cell = ("-" * col1_width) + "-+-" + ("-" * col2_width)
        blank_cell = " " * len(header_cell)

        result_area.insert("end", col_gap.join([header_cell] * num_cols) + "\n")
        result_area.insert("end", col_gap.join([sep_cell] * num_cols) + "\n")

        for row in range(RESULT_ROWS_PER_COLUMN):
            row_cells = []
            row_has_item = False
            for col in range(num_cols):
                idx = col * RESULT_ROWS_PER_COLUMN + row
                if idx < num_items:
                    row_has_item = True
                    num = start_addr + idx
                    val = values[idx]
                    if func_code in (0x01, 0x02):
                        val_str = "ON" if val else "OFF"
                    else:
                        val_str = str(val)
                    cell = f"{num:>{col1_width}} | {val_str:<{col2_width}}"
                else:
                    cell = blank_cell
                row_cells.append(cell)
            if row_has_item:
                result_area.insert("end", col_gap.join(row_cells) + "\n")

        result_area.configure(state="disabled")
        result_area.xview_moveto(x_frac)
    window.after(0, _apply)

def clear_result_with_message(msg):
    def _apply():
        result_area.configure(state="normal")
        result_area.delete("1.0", "end")
        result_area.insert("end", msg)
        result_area.configure(state="disabled")
    window.after(0, _apply)

# --- 포트 갱신 ---

def refresh_ports():
    ports = [p.device for p in serial.tools.list_ports.comports()]
    if not ports:
        ports = ["연결된 포트 없음"]
    port_combo.configure(values=ports)
    port_combo.set(ports[0])

# --- 읽기 요청 실행 ---

# 요청 1회 전송 + 응답 수신 (공용 함수). 프레임 갭 기반으로 끊어 읽음.
def send_and_receive(ser_obj, slave_id, func_code, start_addr, quantity, response_timeout):
    request = build_read_request(slave_id, func_code, start_addr, quantity)
    ser_obj.reset_input_buffer()
    ser_obj.write(request)

    buffer = bytearray()
    deadline = time.time() + response_timeout
    last_recv_time = time.time()

    while time.time() < deadline:
        if ser_obj.in_waiting > 0:
            buffer += ser_obj.read(ser_obj.in_waiting)
            last_recv_time = time.time()
        elif buffer and (time.time() - last_recv_time) >= FRAME_GAP_SEC:
            break
        else:
            time.sleep(0.005)

    return request, bytes(buffer)

# 입력창 값 읽기 + 검증 (실패 시 None 반환)
def read_inputs():
    target_port = port_combo.get()
    if target_port == "연결된 포트 없음":
        log_message("❌ 연결할 UART 장치를 확인하세요.")
        return None
    try:
        baud = int(baud_combo.get())
        slave_id = int(slave_entry.get())
        func_code = FUNCTION_CODES[func_combo.get()]
        start_addr = int(addr_entry.get())
        quantity = int(qty_entry.get())
    except (ValueError, KeyError):
        log_message("❌ 슬레이브ID / 시작주소 / 개수는 숫자로 입력하세요.")
        return None
    if quantity <= 0:
        log_message("❌ 읽을 개수는 1 이상이어야 합니다.")
        return None
    return target_port, baud, slave_id, func_code, start_addr, quantity

def update_error_label():
    count = error_count
    window.after(0, lambda: error_label.configure(text=f"응답 에러: {count}회"))

# [한번 읽기] 버튼: 계속읽기 중이면 정지만 하고, 아니면 1회 읽기 실행
def start_single_read():
    global continuous_running
    if continuous_running:
        continuous_running = False
        return
    t = threading.Thread(target=_do_single_read, daemon=True)
    t.start()

def _do_single_read():
    global ser
    inputs = read_inputs()
    if inputs is None:
        return
    target_port, baud, slave_id, func_code, start_addr, quantity = inputs

    try:
        ser = serial.Serial(target_port, baud, timeout=0.02)
        request, response = send_and_receive(ser, slave_id, func_code, start_addr, quantity, response_timeout=1.5)
        ser.close()

        log_message(f"[송신] {request.hex(' ').upper()}")

        if not response:
            log_message("❌ 응답 없음 (타임아웃). 배선/슬레이브ID/통신속도를 확인하세요.")
            clear_result_with_message("응답 없음")
            return

        log_message(f"[수신] {response.hex(' ').upper()}")

        values, err = parse_response(func_code, response, quantity)
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

# [계속읽기(0.2s/1s)] 버튼: 토글 방식. 다시 누르면 정지
def toggle_continuous():
    global continuous_running, continuous_thread, error_count

    if continuous_running:
        continuous_running = False  # continuous_loop가 스스로 정리하고 버튼을 되돌림
        return

    inputs = read_inputs()
    if inputs is None:
        return
    target_port, baud, slave_id, func_code, start_addr, quantity = inputs

    error_count = 0
    update_error_label()
    continuous_running = True
    continuous_btn.configure(text="■ 정지", fg_color="red", hover_color="darkred")

    continuous_thread = threading.Thread(
        target=continuous_loop,
        args=(target_port, baud, slave_id, func_code, start_addr, quantity),
        daemon=True,
    )
    continuous_thread.start()

def reset_continuous_button():
    window.after(0, lambda: continuous_btn.configure(
        text="계속읽기 (0.2s/1s)", fg_color="green", hover_color="darkgreen"))

def continuous_loop(port, baud, slave_id, func_code, start_addr, quantity):
    global ser, continuous_running, error_count

    try:
        ser = serial.Serial(port, baud, timeout=0.02)
    except Exception as e:
        log_message(f"❌ 포트 열기 실패: {str(e)}")
        continuous_running = False
        reset_continuous_button()
        return

    log_message(f"▶ 계속읽기 시작 ({port}, {baud}bps, 주기 {CONTINUOUS_INTERVAL_SEC}s, 응답대기 {CONTINUOUS_RESPONSE_TIMEOUT}s)")

    while continuous_running:
        cycle_start = time.time()
        try:
            request, response = send_and_receive(
                ser, slave_id, func_code, start_addr, quantity,
                response_timeout=CONTINUOUS_RESPONSE_TIMEOUT,
            )
        except Exception as e:
            log_message(f"❌ 통신 오류: {str(e)}")
            break

        log_message(f"[송신] {request.hex(' ').upper()}")

        if not response:
            error_count += 1
            log_message(f"❌ 응답 없음 (타임아웃) - 누적 오류 {error_count}회")
            update_error_label()
        else:
            log_message(f"[수신] {response.hex(' ').upper()}")
            values, err = parse_response(func_code, response, quantity)
            if err:
                error_count += 1
                log_message(f"❌ {err} - 누적 오류 {error_count}회")
                update_error_label()
            else:
                show_result(func_code, start_addr, values)

        # 0.2초 주기를 맞추되, 정지 요청이 오면 바로 반응하도록 잘게 나눠서 대기
        remain = CONTINUOUS_INTERVAL_SEC - (time.time() - cycle_start)
        while remain > 0 and continuous_running:
            step = min(0.02, remain)
            time.sleep(step)
            remain -= step

    if ser and ser.is_open:
        ser.close()
    log_message("■ 계속읽기 정지")
    reset_continuous_button()

def on_closing():
    global ser, continuous_running
    continuous_running = False
    time.sleep(0.05)
    if ser and ser.is_open:
        ser.close()
    window.destroy()

window.protocol("WM_DELETE_WINDOW", on_closing)

# --- UI 레이아웃 배치 (1024x768 저해상도 화면에서도 스크롤 없이 보이도록 촘촘하게 배치) ---

title_label = ctk.CTkLabel(window, text="Modbus RTU Master", font=("맑은 고딕", 18, "bold"))
title_label.pack(pady=(8, 4))

# 포트/속도 설정
port_frame = ctk.CTkFrame(window, fg_color="transparent")
port_frame.pack(pady=2)

ctk.CTkLabel(port_frame, text="포트:").grid(row=0, column=0, padx=4)
port_combo = ctk.CTkComboBox(port_frame, width=110, height=26)
port_combo.grid(row=0, column=1, padx=4)

ctk.CTkLabel(port_frame, text="속도:").grid(row=0, column=2, padx=4)
baud_combo = ctk.CTkComboBox(port_frame, values=["9600", "19200", "38400", "57600", "115200"], width=90, height=26)
baud_combo.set("9600")
baud_combo.grid(row=0, column=3, padx=4)

btn_refresh = ctk.CTkButton(port_frame, text="🔄 갱신", width=55, height=26, command=refresh_ports)
btn_refresh.grid(row=0, column=4, padx=4)

# 모드버스 요청 설정
modbus_frame = ctk.CTkFrame(window, fg_color="transparent")
modbus_frame.pack(pady=4)

ctk.CTkLabel(modbus_frame, text="슬레이브 ID:").grid(row=0, column=0, padx=4, pady=3, sticky="e")
slave_entry = ctk.CTkEntry(modbus_frame, width=55, height=26)
slave_entry.insert(0, "1")
slave_entry.grid(row=0, column=1, padx=4, pady=3, sticky="w")

ctk.CTkLabel(modbus_frame, text="기능:").grid(row=0, column=2, padx=4, pady=3, sticky="e")
func_combo = ctk.CTkComboBox(modbus_frame, values=list(FUNCTION_CODES.keys()), width=260, height=26)
func_combo.set("03: 홀딩 레지스터 읽기 (Read Holding Registers)")
func_combo.grid(row=0, column=3, padx=4, pady=3, sticky="w")

ctk.CTkLabel(modbus_frame, text="시작 주소:").grid(row=1, column=0, padx=4, pady=3, sticky="e")
addr_entry = ctk.CTkEntry(modbus_frame, width=75, height=26)
addr_entry.insert(0, "0")
addr_entry.grid(row=1, column=1, padx=4, pady=3, sticky="w")

ctk.CTkLabel(modbus_frame, text="개수:").grid(row=1, column=2, padx=4, pady=3, sticky="e")
qty_entry = ctk.CTkEntry(modbus_frame, width=75, height=26)
qty_entry.insert(0, "16")
qty_entry.grid(row=1, column=3, padx=4, pady=3, sticky="w")

btn_frame = ctk.CTkFrame(window, fg_color="transparent")
btn_frame.pack(pady=5)

btn_read = ctk.CTkButton(btn_frame, text="한번 읽기", fg_color="green", hover_color="darkgreen",
                          width=120, height=28, command=start_single_read)
btn_read.grid(row=0, column=0, padx=6)

continuous_btn = ctk.CTkButton(btn_frame, text="계속읽기 (0.2s/1s)", fg_color="green", hover_color="darkgreen",
                                width=160, height=28, command=toggle_continuous)
continuous_btn.grid(row=0, column=1, padx=6)

error_label = ctk.CTkLabel(btn_frame, text="응답 에러: 0회")
error_label.grid(row=0, column=2, padx=12)

# 결과 표시 영역 (기본 10줄까지는 스크롤 없이 세로로, 넘으면 오른쪽에 새 칸)
ctk.CTkLabel(window, text="결과", font=("맑은 고딕", 13, "bold")).pack(pady=(4, 0))
result_area = ctk.CTkTextbox(window, width=660, height=120, font=("Consolas", 12), wrap="none")
result_area.pack(pady=3, padx=10, fill="both", expand=True)
result_area.configure(state="disabled")

# 통신 로그(송/수신 프레임) 표시 영역
# wrap="none": 수신 프레임(HEX)이 길어도 자동으로 줄바꿈되지 않고 한 줄로 유지되며,
# 필요하면 가로 스크롤로 확인합니다.
ctk.CTkLabel(window, text="통신 로그 (HEX)", font=("맑은 고딕", 13, "bold")).pack(pady=(4, 0))
log_area = ctk.CTkTextbox(window, width=660, height=170, font=("Consolas", 11), wrap="none")
log_area.pack(pady=(3, 8), padx=10, fill="x")
log_area.configure(state="disabled")

# 실행 시 사용 가능한 포트 자동 검색
refresh_ports()

window.mainloop()