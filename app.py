import os
import time
from flask import Flask, render_template
from flask_socketio import SocketIO
from web3 import Web3
from collections import deque

# 初始化 Flask
app = Flask(__name__)
# 关键：允许跨域并配置异步模式为 eventlet，这是生产环境长连接的标准
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# --- 链上配置 ---
RPC_URL = "https://mainnet.base.org"
w3 = Web3(Web3.HTTPProvider(RPC_URL))

# 合约地址
TAX_SWAPPER = w3.to_checksum_address("0x8e0253da409faf5918fe2a15979fd878f4495d0e")
AERO_FACTORY = w3.to_checksum_address("0x420DD3807E0e1039f2900483252af73922939021")

# ABI 定义
E_ABI = [{"anonymous":False,"inputs":[{"indexed":True,"name":"token","type":"address"},{"indexed":False,"name":"amount","type":"uint256"}],"name":"SwapTax","type":"event"}]
F_ABI = [{"inputs":[{"internalType":"address","name":"tokenA","type":"address"},{"internalType":"address","name":"tokenB","type":"address"},{"internalType":"bool","name":"stable","type":"bool"}],"name":"getPool","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}]
P_ABI = [{"inputs":[],"name":"getReserves","outputs":[{"internalType":"uint256","name":"_reserve0","type":"uint256"},{"internalType":"uint256","name":"_reserve1","type":"uint256"},{"internalType":"uint256","name":"_blockTimestampLast","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}]

contract = w3.eth.contract(address=TAX_SWAPPER, abi=E_ABI)
factory_contract = w3.eth.contract(address=AERO_FACTORY, abi=F_ABI)

# 历史队列，用于新连接进入时补发数据
history_queue = deque(maxlen=50)
monitor_started = False

def get_aero_data(token_address):
    """获取代币在 Aerodrome 的池子储备量用于计算 Impact 和价格"""
    try:
        token_addr = w3.to_checksum_address(token_address)
        weth_addr = w3.to_checksum_address("0x4200000000000000000000000000000000000006")
        pool_addr = factory_contract.functions.getPool(token_addr, weth_addr, False).call()
        if pool_addr == "0x0000000000000000000000000000000000000000": 
            return 0, 0
        p_c = w3.eth.contract(address=pool_addr, abi=P_ABI)
        res = p_c.functions.getReserves().call()
        t0 = p_c.functions.token0().call()
        if t0.lower() == token_addr.lower():
            return float(w3.from_wei(res[0], 'ether')), float(w3.from_wei(res[1], 'ether'))
        else:
            return float(w3.from_wei(res[1], 'ether')), float(w3.from_wei(res[0], 'ether'))
    except: 
        return 0, 0

def monitor_virtuals_burns():
    """链上事件监控主循环"""
    print("🔎 链上感应器已在云端激活...")
    # 初始回溯 500 个区块（约 15 分钟）
    try:
        last_block = w3.eth.block_number - 500
    except:
        last_block = 0

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
                        "time": time.strftime("%H:%M:%S")
                    }
                    history_queue.append(payload)
                    socketio.emit('new_burn_event', payload)
                    print(f"🔥 检测到销毁: {token_addr[:10]}... 金额: {amount_burn}")
                last_block = current_block
            socketio.sleep(4) # 使用 socketio.sleep 而非 time.sleep 以防阻塞
        except Exception as e:
            print(f"⚠️ 监控遇到干扰: {e}")
            socketio.sleep(10)

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    global monitor_started
    print("📱 客户端已连接")
    # 发送历史数据
    for old in list(history_queue):
        socketio.emit('new_burn_event', old)
    # 仅启动一次后台监控任务
    if not monitor_started:
        socketio.start_background_task(monitor_virtuals_burns)
        monitor_started = True

if __name__ == '__main__':
    # 适配 Railway 动态端口
    port = int(os.environ.get("PORT", 5000))
    # allow_unsafe_werkzeug=True 允许在特定环境下启动，但生产环境建议走 Procfile + gunicorn
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)