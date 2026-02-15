import time
from flask import Flask, render_template
from flask_socketio import SocketIO
from web3 import Web3
from collections import deque

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# --- 核心配置 ---
RPC_URL = "https://mainnet.base.org"
w3 = Web3(Web3.HTTPProvider(RPC_URL))

# 合约地址
TAX_SWAPPER = w3.to_checksum_address("0x8e0253da409faf5918fe2a15979fd878f4495d0e")
AERO_FACTORY = w3.to_checksum_address("0x420DD3807E0e1039f2900483252af73922939021")

# ABI (保持不变)
E_ABI = [{"anonymous":False,"inputs":[{"indexed":True,"name":"token","type":"address"},{"indexed":False,"name":"amount","type":"uint256"}],"name":"SwapTax","type":"event"}]
F_ABI = [{"inputs":[{"internalType":"address","name":"tokenA","type":"address"},{"internalType":"address","name":"tokenB","type":"address"},{"internalType":"bool","name":"stable","type":"bool"}],"name":"getPool","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}]
P_ABI = [{"inputs":[],"name":"getReserves","outputs":[{"internalType":"uint256","name":"_reserve0","type":"uint256"},{"internalType":"uint256","name":"_reserve1","type":"uint256"},{"internalType":"uint256","name":"_blockTimestampLast","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}]

contract = w3.eth.contract(address=TAX_SWAPPER, abi=E_ABI)
factory_contract = w3.eth.contract(address=AERO_FACTORY, abi=F_ABI)

history_queue = deque(maxlen=50)
monitor_started = False

def get_aero_data(token_address):
    try:
        token_addr = w3.to_checksum_address(token_address)
        weth_addr = w3.to_checksum_address("0x4200000000000000000000000000000000000006")
        pool_addr = factory_contract.functions.getPool(token_addr, weth_addr, False).call()
        if pool_addr == "0x0000000000000000000000000000000000000000": return 0, 0
        p_c = w3.eth.contract(address=pool_addr, abi=P_ABI)
        res = p_c.functions.getReserves().call()
        t0 = p_c.functions.token0().call()
        # 获取代币储备和对应的 WETH 储备（用于前端估值）
        if t0.lower() == token_addr.lower():
            return float(w3.from_wei(res[0], 'ether')), float(w3.from_wei(res[1], 'ether'))
        else:
            return float(w3.from_wei(res[1], 'ether')), float(w3.from_wei(res[0], 'ether'))
    except: return 0, 0

def monitor_virtuals_burns():
    print("🔎 感应器已启动...")
    last_block = w3.eth.block_number - 500 # 追溯约15分钟
    while True:
        try:
            current_block = w3.eth.block_number
            if current_block > last_block:
                events = contract.events.SwapTax().get_logs(from_block=last_block + 1, to_block=current_block)
                for event in events:
                    token_addr = event.args.token
                    amount_burn = float(w3.from_wei(event.args.amount, 'ether'))
                    
                    # 获取池子详细数据
                    token_res, weth_res = get_aero_data(token_addr)
                    impact = (amount_burn / token_res * 100) if token_res > 0 else 0
                    
                    payload = {
                        "token": token_addr,
                        "amount": f"{amount_burn:,.2f}",
                        "reserve": f"{token_res:,.2f}",
                        "weth_reserve": weth_res, # 传入 WETH 数量辅助前端算价
                        "impact": round(impact, 4),
                        "tx": event.transactionHash.hex(),
                        "time": time.strftime("%H:%M:%S")
                    }
                    history_queue.append(payload)
                    socketio.emit('new_burn_event', payload)
                last_block = current_block
            socketio.sleep(4)
        except: socketio.sleep(10)

@app.route('/')
def index(): return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    global monitor_started
    for old in list(history_queue): socketio.emit('new_burn_event', old)
    if not monitor_started:
        socketio.start_background_task(monitor_virtuals_burns)
        monitor_started = True

if __name__ == '__main__':
    socketio.run(app, host='127.0.0.1', port=5000, debug=False)