import threading
import time
import customtkinter as ctk
import serial
import serial.tools.list_ports
 
# 글로벌 변수 설정
ser = None
running = False
 
# 데이터가 이 시간(초) 이상 안 들어오면 "한 프레임이 끝났다"고 보고 화면에 출력합니다.
# \n(0x0A) 같은 특정 바이트로 자르지 않기 때문에 바이너리 데이터가 섞여도 끊기지 않습니다.
FRAME_GAP_SEC = 0.05
 
# --- GUI 화면 설정 ---
ctk.set_appearance_mode("System")  # 윈도우 모드(다크/라이트) 자동 인식
window = ctk.CTk()
window.title("실시간 UART 데이터 모니터")
window.geometry("600x540")
 
# --- 기능 함수 정의 ---
 
# 텍스트 창에 수신된 데이터를 출력하는 함수
def log_message(message):
    text_area.configure(state="normal")  # 편집 가능 상태로 전환
    text_area.insert("end", message + "\n")  # 데이터 삽입
    text_area.see("end")  # 스크롤을 항상 맨 아래로
    text_area.configure(state="disabled")  # 다시 읽기 전용으로 잠금
 
# 현재 PC에 연결된 COM 포트 목록을 갱신하는 함수
def refresh_ports():
    ports = [p.device for p in serial.tools.list_ports.comports()]
    if not ports:
        ports = ["연결된 포트 없음"]
    port_combo.configure(values=ports)
    port_combo.set(ports[0])
 
# 수신된 바이트를 현재 선택된 표시 모드(ASCII/HEX)에 맞게 문자열로 변환
def format_received(raw_bytes):
    mode = display_mode_var.get()
    if mode == "HEX":
        # 바이트를 공백으로 구분된 16진수 문자열로 표시 (예: 41 42 0D 0A)
        return " ".join(f"{b:02X}" for b in raw_bytes)
    else:
        # ASCII 모드: 디코드 불가능한 바이트는 무시하고 표시
        return raw_bytes.decode("utf-8", errors="ignore").strip()
 
# 백그라운드에서 실시간으로 UART 데이터를 읽는 함수 (스레드용)
def read_uart(port, baudrate):
    global ser, running
    try:
        # 시리얼 포트 열기 (timeout은 데이터가 안 들어올 때 대기 시간)
        ser = serial.Serial(port, baudrate, timeout=0.05)
        running = True
        log_message(f"✅ {port} 연결 성공! (통신속도: {baudrate})")
 
        buffer = bytearray()
        last_recv_time = time.time()
 
        while running:
            if ser.in_waiting > 0:
                # \n 같은 특정 바이트로 자르지 않고, 들어온 만큼 그대로 버퍼에 누적합니다.
                buffer += ser.read(ser.in_waiting)
                last_recv_time = time.time()
            elif buffer and (time.time() - last_recv_time) >= FRAME_GAP_SEC:
                # 일정 시간 동안 새 데이터가 없으면 한 프레임이 끝난 것으로 보고 출력합니다.
                text = format_received(bytes(buffer))
                if text:
                    log_message(f"[수신] {text}")
                buffer = bytearray()
            else:
                time.sleep(0.005)
 
    except Exception as e:
        log_message(f"❌ 오류 발생: {str(e)}")
    finally:
        if ser and ser.is_open:
            ser.close()
        running = False
        log_message("🔌 통신이 안전하게 종료되었습니다.")
 
# [연결 시작] 버튼 클릭 시
def start_connection():
    global running
    if running:
        return
 
    target_port = port_combo.get()
    if target_port == "연결된 포트 없음":
        log_message("❌ 연결할 UART 장치를 확인하세요.")
        return
 
    target_baud = int(baud_combo.get())
 
    # GUI가 멈추지 않도록 통신 로직은 별도의 스레드(일꾼)로 실행합니다.
    t = threading.Thread(target=read_uart, args=(target_port, target_baud), daemon=True)
    t.start()
 
# [연결 종료] 버튼 클릭 시
def stop_connection():
    global running
    running = False
 
# 프로그램 창을 닫을 때 안전하게 포트 닫기
def on_closing():
    global running
    running = False
    window.destroy()
 
window.protocol("WM_DELETE_WINDOW", on_closing)
 
# --- UI 레이아웃 배치 ---
 
title_label = ctk.CTkLabel(window, text="UART Data Viewer", font=("맑은 고딕", 24, "bold"))
title_label.pack(pady=15)
 
# 상단 설정 바 (포트 선택, 속도 선택)
config_frame = ctk.CTkFrame(window, fg_color="transparent")
config_frame.pack(pady=5)
 
ctk.CTkLabel(config_frame, text="포트:").grid(row=0, column=0, padx=5)
port_combo = ctk.CTkComboBox(config_frame, width=120)
port_combo.grid(row=0, column=1, padx=5)
 
ctk.CTkLabel(config_frame, text="속도:").grid(row=0, column=2, padx=5)
baud_combo = ctk.CTkComboBox(config_frame, values=["9600", "115200", "4800", "19200", "38400"], width=100)
baud_combo.set("9600")  # 기본값 9600
baud_combo.grid(row=0, column=3, padx=5)
 
btn_refresh = ctk.CTkButton(config_frame, text="🔄 갱신", width=60, command=refresh_ports)
btn_refresh.grid(row=0, column=4, padx=5)
 
# 표시 모드 선택 바 (ASCII / HEX)
mode_frame = ctk.CTkFrame(window, fg_color="transparent")
mode_frame.pack(pady=5)
 
ctk.CTkLabel(mode_frame, text="표시 형식:").grid(row=0, column=0, padx=5)
display_mode_var = ctk.StringVar(value="ASCII")
mode_segment = ctk.CTkSegmentedButton(
    mode_frame,
    values=["ASCII", "HEX"],
    variable=display_mode_var,
)
mode_segment.grid(row=0, column=1, padx=5)
 
# 실시간 데이터를 보여줄 텍스트 상자
text_area = ctk.CTkTextbox(window, width=540, height=280, font=("Consolas", 14))
text_area.pack(pady=15)
text_area.configure(state="disabled")
 
# 제어 버튼 (시작 / 종료)
btn_frame = ctk.CTkFrame(window, fg_color="transparent")
btn_frame.pack(pady=5)
 
btn_start = ctk.CTkButton(btn_frame, text="연결 시작", fg_color="green", hover_color="darkgreen", command=start_connection)
btn_start.grid(row=0, column=0, padx=15)
 
btn_stop = ctk.CTkButton(btn_frame, text="연결 종료", fg_color="red", hover_color="darkred", command=stop_connection)
btn_stop.grid(row=0, column=1, padx=15)
 
# 실행 시 사용 가능한 포트 자동 검색
refresh_ports()
 
# GUI 루프 시작
window.mainloop()