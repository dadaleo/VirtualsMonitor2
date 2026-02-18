import os
import time
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template
from flask_socketio import SocketIO
from web3 import Web3
from collections import deque

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# --- 环境变量与配置 ---
RPC_URL = "https://mainnet.base.org"
# Railway 会自动注入 DATABASE_URL，如果没有则使用内存模拟（本地测试用）
DATABASE_URL = os.environ.get('DATABASE_URL')
w3 = Web3(Web3.HTTPProvider(RPC_URL))

# 合约地址配置
TAX_SWAPPER = w3.to_checksum_address("0x8e0253da409faf5918fe2a15979fd878f4495d0e")
AERO_FACTORY = w3.to_checksum_address("0x420DD3807E0e1039f2900483252af73922939021")

# ABI 配置
E_ABI = [{"anonymous":False,"inputs":[{"indexed":True,"name":"token","type":"address"},{"indexed":False,"name":"amount","type":"uint256"}],"name":"SwapTax","type":"event"}]
F_ABI = [{"inputs":[{"internalType":"address","name":"tokenA","type":"address"},{"internalType":"address","name":"tokenB","type":"address"},{"internalType":"bool","name":"stable","type":"bool"}],"name":"getPool","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}]
P_ABI = [{"inputs":[],"name":"getReserves","outputs":[{"internalType":"uint256","name":"_reserve0","type":"uint256"},{"internalType":"uint256","name":"_reserve1","type":"uint256"},{"internalType":"uint256","name":"_blockTimestampLast","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}]

contract = w3.eth.contract(address=TAX_SWAPPER, abi=E_ABI)
factory_contract = w3.eth.contract(address=AERO_FACTORY, abi=F_ABI)
monitor_started = False

# --- 数据库操作函数 ---
def init_db():
    """初始化数据库表结构"""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS burns (
                tx TEXT PRIMARY KEY,
                token TEXT,
                symbol TEXT,
                amount TEXT,
                usd REAL,
                impact REAL,
                time TEXT,
                fdv REAL
            )
        ''')
        conn.commit()
        cur.close()
        conn.close()
        print("✅ 数据库初始化成功")
    except Exception as e:
        print(f"❌ 数据库初始化失败: {e}")

def save_to_db(e):
    """将销毁事件存入数据库"""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO burns (tx, token, symbol, amount, usd, impact, time, fdv)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tx) DO NOTHING
        ''', (e['tx'], e['token'], e['symbol'], e['amount'], e['usd'], e['impact'], e['time'], e['fdv']))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as err:
        print(f"❌ 数据库写入错误: {err}")

# --- 核心逻辑函数 ---
def get_aero_data(token_address):
    """获取流动性池数据"""
    try:
        token_addr = w3.to_checksum_address(token_address)
        weth_addr = w3.to_checksum_address("0x4200000000000000000000000000000000000006")
        # 同时尝试波动池和稳定池
        pool_addr = factory_contract.functions.getPool(token_addr, weth_addr, False).call()
        if pool_addr == "0x0000000000000000000000000000000000000000":
            pool_addr = factory_contract.functions.getPool(token_addr, weth_addr, True).call()
            
        if pool_addr == "0x0000000000000000000000000000000000000000": return 0, 0
        p_c = w3.eth.contract(address=pool_addr, abi=P_ABI)
        res = p_c.functions.getReserves().call()
        t0 = p_c.functions.token0().call()
        if t0.lower() == token_addr.lower():
            return float(w3.from_wei(res[0], 'ether')), float(w3.from_wei(res[1], 'ether'))
        else:
            return float(w3.from_wei(res[1], 'ether')), float(w3.from_wei(res[0], 'ether'))
    except: return 0, 0

def monitor_virtuals_burns():
    """后台监控线程"""
    global monitor_started
    init_db()
    # 启动时回溯一小段，确保平滑衔接
    last_block = w3.eth.block_number - 50 
    
    while True:
        try:
            current_block = w3.eth.block_number
            if current_block > last_block:
                events = contract.events.SwapTax().get_logs(from_block=last_block + 1, to_block=current_block)
                for event in events:
                    token_addr = event.args.token
                    amount_burn = float(w3.from_wei(event.args.amount, 'ether'))
                    t_res, w_res = get_aero_data(token_addr)
                    impact = (amount_burn / t_res * 100) if t_res > 0 else 0
                    
                    payload = {
                        "token": token_addr, 
                        "amount": f"{amount_burn:,.2f}",
                        "reserve": f"{t_res:,.2f}", 
                        "weth_reserve": w_res,
                        "impact": round(impact, 4), 
                        "tx": event.transactionHash.hex(),
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "symbol": "Scanning...", # 前端会通过 DexScreener 补全
                        "fdv": 0
                    }
                    
                    # 存入数据库并推送
                    save_to_db(payload)
                    socketio.emit('new_burn_event', payload)
                last_block = current_block
            socketio.sleep(5)
        except Exception as e:
            print(f"Monitor Error: {e}")
            socketio.sleep(10)

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    """当新客户端连接时，从数据库提取最近 200 条数据发送"""
    global monitor_started
    try:
        if DATABASE_URL:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor(cursor_factory=RealDictCursor)
            # 提取最近 200 条
            cur.execute("SELECT * FROM burns ORDER BY time DESC LIMIT 200")
            rows = cur.fetchall()
            # 逆序发送，让前端按时间线堆叠
            for row in reversed(rows):
                socketio.emit('new_burn_event', dict(row))
            cur.close()
            conn.close()
    except Exception as e:
        print(f"History Load Error: {e}")

    if not monitor_started:
        socketio.start_background_task(monitor_virtuals_burns)
        monitor_started = True

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)