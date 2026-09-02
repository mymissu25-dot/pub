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

# 여러 스레드(계속읽기 / 쓰기)가 동시에 같은 시리얼 포트에 접근하지 못하도록 막는 잠금.
# 이 락을 잡은 스레드만 요청을 보내고 응답을 다 받을 때까지 포트를 독점합니다.
serial_lock = threading.Lock()

# 응답 프레임을 받을 때, 이 시간(초) 이상 새 데이터가 안 들어오면 응답이 끝난 것으로 간주합니다.
FRAME_GAP_SEC = 0.1  # 0.05 → 0.1로 증가 (응답 완전히 받을 시간 확보)

# 계속읽기 모드 설정
CONTINUOUS_INTERVAL_SEC = 0.2   # 요청 주기
CONTINUOUS_RESPONSE_TIMEOUT = 1.5  # 1.0 → 1.5로 증가

# 계속읽기를 잠시 멈출 때, 실제로 스레드가 종료됐는지 기다리는 최대 시간
CONTINUOUS_STOP_JOIN_TIMEOUT = CONTINUOUS_RESPONSE_TIMEOUT + 1.0

# 결과창에서 세로로 몇 줄 채우면 오른쪽에 새 칸을 시작할지
RESULT_ROWS_PER_COLUMN = 10

# 지원할 모드버스 기능 코드
# 주의: 06(단일 레지스터 쓰기)은 여기 목록에 넣지 않습니다. "한번 읽기"/"계속읽기" 버튼은
# 항상 읽기 프레임만 생성하기 때문에, 콤보박스에서 06을 선택해 "읽기"를 누르면 엉뚱한
# 쓰기 명령이 나가버립니다. 쓰기는 03으로 읽은 결과에서 값을 클릭해 여는 팝업으로만 합니다.
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
window.title("모드버스 RTU 마스터 (읽기/쓰기)")
window.geometry("700x640")
window.minsize(680, 640)

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

# 읽기 요청 프레임 생성
def build_read_request(slave_id, func_code, start_addr, quantity):
    frame = bytes([slave_id, func_code])
    frame += start_addr.to_bytes(2, "big")
    frame += quantity.to_bytes(2, "big")
    frame += modbus_crc16(frame)
    return frame

# 쓰기 요청 프레임 생성 (06번)
def build_write_request(slave_id, address, value):
    frame = bytes([slave_id, 0x06])
    frame += address.to_bytes(2, "big")
    frame += value.to_bytes(2, "big")
    frame += modbus_crc16(frame)
    return frame

# CRC 검증
def check_crc(frame: bytes) -> bool:
    if len(frame) < 3:
        return False
    body, crc_recv = frame[:-2], frame[-2:]
    return modbus_crc16(body) == crc_recv

# 응답 프레임 파싱 (CRC 오류 시 더 자세한 정보)
def parse_response(func_code, resp: bytes, quantity):
    if len(resp) < 5:
        return None, f"응답 길이가 너무 짧습니다. (수신: {len(resp)}바이트)"

    if not check_crc(resp):
        return None, f"CRC 오류 (수신 프레임: {resp.hex(' ').upper()})"

    fc = resp[1]
    if fc & 0x80:
        exc_code = resp[2]
        msg = MODBUS_EXCEPTIONS.get(exc_code, f"알 수 없는 예외 코드 {exc_code}")
        return None, f"모드버스 예외 응답: {msg}"

    if func_code == 0x06:
        if len(resp) >= 8:
            address = int.from_bytes(resp[2:4], "big")
            value = int.from_bytes(resp[4:6], "big")
            return [value], None
        else:
            return None, "쓰기 응답 길이 오류"

    byte_count = resp[2]
    data = resp[3:3 + byte_count]

    if func_code in (0x01, 0x02):
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

    elif func_code in (0x03, 0x04):
        values = []
        for i in range(0, byte_count - 1, 2):
            val = (data[i] << 8) | data[i + 1]
            values.append(val)
        return values, None

    return None, "지원하지 않는 기능 코드입니다."

# --- 로그/결과 출력 함수 ---
def log_message(message):
    def _apply():
        log_area.configure(state="normal")
        log_area.insert("end", message + "\n")
        log_area.yview_moveto(1.0)
        log_area.configure(state="disabled")
    window.after(0, _apply)

def show_result(func_code, start_addr, values):
    def _apply():
        x_frac = result_area.xview()[0]

        result_area.configure(state="normal")
        result_area.delete("1.0", "end")

        col1_width = 6
        col2_width = 10
        col_gap = "   "

        num_items = len(values)
        num_cols = max(1, -(-num_items // RESULT_ROWS_PER_COLUMN))

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

                    cell_text = f"{num:>{col1_width}} | {val_str:<{col2_width}}"
                    cell_start = result_area.index("end-1c")
                    result_area.insert("end", cell_text)
                    cell_end = result_area.index("end-1c")

                    tag_name = f"clickable_{num}"
                    result_area.tag_add(tag_name, cell_start, cell_end)
                    result_area.tag_config(tag_name, foreground="blue")
                    result_area.tag_bind(tag_name, "<Button-1>",
                                        lambda e, addr=num, val=val, fc=func_code: on_value_click(addr, val, fc))
                else:
                    result_area.insert("end", blank_cell)
            if row_has_item:
                result_area.insert("end", "\n")

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

# --- 값 클릭 시 팝업창 (크게, 확인 버튼만) ---
def on_value_click(address, current_value, func_code):
    if func_code != 0x03:
        log_message(f"⚠️ 주소 {address}는 쓰기 불가능한 타입입니다. (03번으로 읽은 값만 쓰기 가능)")
        return

    popup = ctk.CTkToplevel(window)
    popup.title(f"레지스터 쓰기 - 주소 {address}")
    popup.geometry("450x300")  # 크기 증가
    popup.resizable(False, False)
    popup.grab_set()

    # 팝업창 내용
    ctk.CTkLabel(popup, text=f"주소 {address}에 값 쓰기", font=("맑은 고딕", 18, "bold")).pack(pady=(25, 5))
    ctk.CTkLabel(popup, text=f"현재 값: {current_value}", font=("맑은 고딕", 14)).pack(pady=(0, 15))

    # 값 입력창
    value_frame = ctk.CTkFrame(popup, fg_color="transparent")
    value_frame.pack(pady=10)
    ctk.CTkLabel(value_frame, text="새 값 (0~65535):", font=("맑은 고딕", 14)).pack(side="left", padx=10)
    value_entry = ctk.CTkEntry(value_frame, width=180, height=40, font=("맑은 고딕", 16))
    value_entry.insert(0, str(current_value))
    value_entry.pack(side="left", padx=10)

    # 상태 표시 레이블
    status_label = ctk.CTkLabel(popup, text="", font=("맑은 고딕", 13))
    status_label.pack(pady=15)

    def do_write():
        try:
            new_value = int(value_entry.get())
            if new_value < 0 or new_value > 65535:
                status_label.configure(text="❌ 0~65535 범위의 값을 입력하세요.", text_color="red")
                return

            status_label.configure(text="⏳ 쓰는 중...", text_color="gray")
            popup.update_idletasks()

            success = write_single_register(address, new_value)
            if success:
                status_label.configure(text="✅ 쓰기 성공! 창을 닫아주세요.", text_color="green")
                refresh_result()
                popup.after(1500, popup.destroy)
            else:
                status_label.configure(text="❌ 쓰기 실패! 로그를 확인하세요.", text_color="red")
        except ValueError:
            status_label.configure(text="❌ 숫자를 입력하세요.", text_color="red")

    # 버튼 프레임
    btn_frame = ctk.CTkFrame(popup, fg_color="transparent")
    btn_frame.pack(pady=20)

    # 확인 버튼 (더 크게)
    confirm_btn = ctk.CTkButton(btn_frame, text="✅ 확인", width=200, height=60,
                                fg_color="#2ecc71", hover_color="#27ae60",
                                font=("맑은 고딕", 18, "bold"),
                                corner_radius=12,
                                command=do_write)
    confirm_btn.pack(padx=10)

    # Enter 키로 확인 버튼 동작
    value_entry.bind("<Return>", lambda e: do_write())

    # 입력창에 포커스
    value_entry.focus_set()

# --- 쓰기 함수 ---
def write_single_register(address, value):
    global ser, continuous_running, continuous_thread

    was_continuous = continuous_running

    if was_continuous:
        log_message("⏸️ 계속읽기 일시 중지 중...")
        continuous_running = False
        # sleep이 아니라 join으로 실제 스레드 종료를 확인합니다.
        # (계속읽기 스레드가 응답을 기다리는 도중일 수 있어 최대 응답대기시간만큼은 걸릴 수 있습니다.)
        if continuous_thread is not None:
            continuous_thread.join(timeout=CONTINUOUS_STOP_JOIN_TIMEOUT)
            if continuous_thread.is_alive():
                log_message("⚠️ 계속읽기 스레드 정지 확인 시간 초과. 통신은 잠금으로 보호되니 계속 진행합니다.")

    inputs = read_inputs()
    if inputs is None:
        if was_continuous:
            continuous_running = True
        return False

    target_port, baud, slave_id, _, _, _ = inputs

    try:
        # 실제 포트 입출력은 반드시 잠금 안에서만 수행 (계속읽기와 절대 겹치지 않도록)
        with serial_lock:
            if ser is not None and ser.is_open:
                log_message(f"🔗 기존 포트 {target_port} 재사용")
            else:
                log_message(f"🔌 포트 {target_port} 열기...")
                ser = serial.Serial(target_port, baud, timeout=0.5)

            request = build_write_request(slave_id, address, value)
            ser.reset_input_buffer()
            ser.write(request)

            log_message(f"[쓰기 송신] {request.hex(' ').upper()}")

            # 응답 대기 (충분히 대기)
            time.sleep(0.1)
            response = ser.read(8)

        if not response:
            log_message("❌ 쓰기 응답 없음 (타임아웃)")
            return False

        log_message(f"[쓰기 수신] {response.hex(' ').upper()}")

        _, err = parse_response(0x06, response, 1)
        if err:
            log_message(f"❌ 쓰기 오류: {err}")
            return False

        log_message(f"✅ 주소 {address}에 값 {value} 쓰기 성공!")
        return True

    except PermissionError:
        log_message(f"❌ 포트 {target_port} 사용 중! 다른 프로그램을 종료하세요.")
        return False
    except Exception as e:
        log_message(f"❌ 쓰기 오류 발생: {str(e)}")
        return False
    finally:
        if was_continuous:
            try:
                slave_id = int(slave_entry.get())
                func_code = FUNCTION_CODES[func_combo.get()]
                start_addr = int(addr_entry.get())
                quantity = int(qty_entry.get())

                continuous_running = True
                continuous_btn.configure(text="■ 정지", fg_color="red", hover_color="darkred")

                continuous_thread = threading.Thread(
                    target=continuous_loop_with_existing_ser,
                    args=(target_port, baud, slave_id, func_code, start_addr, quantity),
                    daemon=True,
                )
                continuous_thread.start()
                log_message("▶ 계속읽기 재개")
            except Exception as e:
                log_message(f"❌ 계속읽기 재개 실패: {str(e)}")
                continuous_running = False
                reset_continuous_button()

def refresh_result():
    if continuous_running:
        return
    threading.Thread(target=_do_single_read, daemon=True).start()

# --- 포트 갱신 ---
def refresh_ports():
    ports = [p.device for p in serial.tools.list_ports.comports()]
    if not ports:
        ports = ["연결된 포트 없음"]
    port_combo.configure(values=ports)
    port_combo.set(ports[0])

# --- 읽기 요청 실행 (개선된 버전) ---
def send_and_receive(ser_obj, slave_id, func_code, start_addr, quantity, response_timeout):
    # 요청 전송 + 응답 수신 전체를 하나의 원자적 트랜잭션으로 취급합니다.
    # 이 락이 걸려있는 동안은 다른 스레드(쓰기 등)가 같은 포트에 절대 접근할 수 없습니다.
    with serial_lock:
        request = build_read_request(slave_id, func_code, start_addr, quantity)
        ser_obj.reset_input_buffer()
        ser_obj.write(request)

        buffer = bytearray()
        deadline = time.time() + response_timeout
        last_recv_time = time.time()

        # 예상 응답 길이 (함수 코드별로 다름: 01/02는 비트단위, 03/04는 레지스터단위)
        if func_code in (0x01, 0x02):
            data_bytes = (quantity + 7) // 8
        else:
            data_bytes = quantity * 2
        expected_len = 5 + data_bytes

        while time.time() < deadline:
            if ser_obj.in_waiting > 0:
                buffer += ser_obj.read(ser_obj.in_waiting)
                last_recv_time = time.time()

                # 예상 길이만큼 받았으면 잠시 더 기다렸다가(혹시 남은 바이트) 종료
                if len(buffer) >= expected_len:
                    time.sleep(0.02)
                    if ser_obj.in_waiting > 0:
                        buffer += ser_obj.read(ser_obj.in_waiting)
                    break

            elif buffer and (time.time() - last_recv_time) >= FRAME_GAP_SEC:
                break
            else:
                time.sleep(0.01)

    return request, bytes(buffer)

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

    for attempt in range(3):
        try:
            ser = serial.Serial(target_port, baud, timeout=0.02)
            break
        except PermissionError:
            log_message(f"⚠️ 포트 {target_port} 사용 중... 재시도 {attempt+1}/3")
            time.sleep(1)
        except Exception as e:
            log_message(f"❌ 포트 열기 실패: {str(e)}")
            return

    if ser is None or not ser.is_open:
        log_message("❌ 포트를 열 수 없습니다.")
        return

    try:
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

def toggle_continuous():
    global continuous_running, continuous_thread, error_count

    if continuous_running:
        continuous_running = False
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

        remain = CONTINUOUS_INTERVAL_SEC - (time.time() - cycle_start)
        while remain > 0 and continuous_running:
            step = min(0.02, remain)
            time.sleep(step)
            remain -= step

    if ser and ser.is_open:
        ser.close()
    log_message("■ 계속읽기 정지")
    reset_continuous_button()

# 기존 ser 객체를 재사용하는 계속읽기 루프
def continuous_loop_with_existing_ser(port, baud, slave_id, func_code, start_addr, quantity):
    global ser, continuous_running, error_count

    if ser is None or not ser.is_open:
        try:
            ser = serial.Serial(port, baud, timeout=0.02)
        except Exception as e:
            log_message(f"❌ 포트 열기 실패: {str(e)}")
            continuous_running = False
            reset_continuous_button()
            return

    log_message(f"▶ 계속읽기 재개 ({port}, {baud}bps)")

    while continuous_running:
        cycle_start = time.time()
        try:
            if ser is None or not ser.is_open:
                log_message("❌ 포트가 닫혔습니다. 계속읽기 중지")
                break

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

# --- UI 레이아웃 배치 ---
title_label = ctk.CTkLabel(window, text="Modbus RTU Master", font=("맑은 고딕", 18, "bold"))
title_label.pack(pady=(8, 4))

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

ctk.CTkLabel(window, text="결과 (값을 클릭하면 쓰기 팝업이 열립니다)", font=("맑은 고딕", 13, "bold")).pack(pady=(4, 0))
result_area = ctk.CTkTextbox(window, width=660, height=120, font=("Consolas", 12), wrap="none")
result_area.pack(pady=3, padx=10, fill="both", expand=True)
result_area.configure(state="disabled")

ctk.CTkLabel(window, text="통신 로그 (HEX)", font=("맑은 고딕", 13, "bold")).pack(pady=(4, 0))
log_area = ctk.CTkTextbox(window, width=660, height=170, font=("Consolas", 11), wrap="none")
log_area.pack(pady=(3, 8), padx=10, fill="x")
log_area.configure(state="disabled")

refresh_ports()
window.mainloop()