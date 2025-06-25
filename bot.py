import os
import time
from threading import Thread
from flask import Flask
from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import geth_poa_middleware

load_dotenv()

# PancakeSwap v2 Router
PANCAKE_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
USDT = Web3.to_checksum_address("0x55d398326f99059fF775485246999027B3197955")
BUSD = Web3.to_checksum_address("0xe9e7cea3dedca5984780Bafc599bD69aDd087D56")

ROUTER_ABI = [
    {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"address[]","name":"path","type":"address[]"}],"name":"getAmountsOut","outputs":[{"internalType":"uint256[]","name":"","type":"uint256[]"}],"stateMutability":"view","type":"function"},
]

# Load your deployed flash loan contract ABI and address from environment or file
FLASHLOAN_CONTRACT_ADDRESS = Web3.to_checksum_address(os.getenv("FLASHLOAN_CONTRACT_ADDRESS"))
with open("FlashLoanArb.abi") as f:
    FLASHLOAN_CONTRACT_ABI = f.read()

class FlashLoanArbBot:
    def __init__(self):
        self.BSC_RPC = os.getenv("BSC_RPC", "https://bsc-dataseed.binance.org/")
        self.web3 = Web3(Web3.HTTPProvider(self.BSC_RPC))
        self.web3.middleware_onion.inject(geth_poa_middleware, layer=0)
        self.private_key = os.getenv("PRIVATE_KEY")
        if not self.private_key:
            raise ValueError("PRIVATE_KEY environment variable not set.")
        self.account = self.web3.eth.account.from_key(self.private_key)
        self.address = self.account.address

        self.router = self.web3.eth.contract(address=PANCAKE_ROUTER, abi=ROUTER_ABI)
        self.flashloan_contract = self.web3.eth.contract(
            address=FLASHLOAN_CONTRACT_ADDRESS,
            abi=FLASHLOAN_CONTRACT_ABI
        )

        # User settings
        self.min_loan = int(100 * 10**18)   # Try with 100 USDT (change as needed)
        self.max_loan = int(5000 * 10**18)  # Max 5000 USDT (change as needed)
        self.loan_step = int(100 * 10**18)  # Step size for loan search
        self.flashloan_fee = 0.0009         # 0.09% for DODO
        self.gas_limit = 600000             # Estimate for flash loan arb
        self.slippage = 0.002               # 0.2% slippage

    def get_amount_out(self, amount_in, path):
        try:
            amounts = self.router.functions.getAmountsOut(amount_in, path).call()
            return amounts[-1]
        except Exception as e:
            print(f"Error getting amount out: {e}")
            return 0

    def get_bnb_usdt_price(self):
        # Get BNB price in USDT using PancakeSwap
        try:
            wbnb = Web3.to_checksum_address("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c")
            amounts = self.router.functions.getAmountsOut(10**18, [wbnb, USDT]).call()
            return amounts[-1] / 1e18
        except Exception as e:
            print(f"Error getting BNB price: {e}")
            return 600  # fallback

    def estimate_gas_cost_usdt(self):
        gas_price = self.web3.eth.gas_price
        bnb_usdt = self.get_bnb_usdt_price()
        return self.gas_limit * gas_price / 1e18 * bnb_usdt

    def expected_profit(self, loan_amount, direction):
        # direction: True = USDT->BUSD->USDT, False = BUSD->USDT->BUSD
        if direction:
            out1 = self.get_amount_out(loan_amount, [USDT, BUSD])
            out2 = self.get_amount_out(out1, [BUSD, USDT])
        else:
            out1 = self.get_amount_out(loan_amount, [BUSD, USDT])
            out2 = self.get_amount_out(out1, [USDT, BUSD])
        flash_fee = loan_amount * self.flashloan_fee
        gas_cost = self.estimate_gas_cost_usdt()
        profit = out2 - loan_amount - flash_fee - gas_cost
        print(f"Loan: {loan_amount/1e18:.2f}, Dir: {'USDT→BUSD→USDT' if direction else 'BUSD→USDT→BUSD'}, "
              f"Profit: {profit/1e18:.6f} USDT, Gas: {gas_cost:.4f} USDT, Fee: {flash_fee/1e18:.6f} USDT")
        return profit

    def find_best_opportunity(self):
        best_profit = 0
        best_amount = 0
        best_direction = True
        for direction in [True, False]:
            for loan in range(self.min_loan, self.max_loan + self.loan_step, self.loan_step):
                profit = self.expected_profit(loan, direction)
                if profit > best_profit:
                    best_profit = profit
                    best_amount = loan
                    best_direction = direction
        return best_profit, best_amount, best_direction

    def execute_flashloan(self, loan_amount, direction):
        print(f"Executing flash loan: {loan_amount/1e18:.2f} USDT, Direction: {'USDT→BUSD→USDT' if direction else 'BUSD→USDT→BUSD'}")
        nonce = self.web3.eth.get_transaction_count(self.address)
        tx = self.flashloan_contract.functions.executeArbitrage(
            loan_amount, direction
        ).build_transaction({
            'from': self.address,
            'nonce': nonce,
            'gas': self.gas_limit,
            'gasPrice': self.web3.eth.gas_price,
        })
        signed = self.web3.eth.account.sign_transaction(tx, self.private_key)
        tx_hash = self.web3.eth.send_raw_transaction(signed.rawTransaction)
        print(f"Flash loan TX: {self.web3.to_hex(tx_hash)}")
        receipt = self.web3.eth.wait_for_transaction_receipt(tx_hash)
        print("Transaction receipt:", receipt)
        return receipt.status == 1

    def run(self):
        print("Flash Loan Arbitrage Bot started.")
        while True:
            try:
                profit, amount, direction = self.find_best_opportunity()
                if profit > 0:
                    print(f"Profitable opportunity found! Profit: {profit/1e18:.6f} USDT (loan: {amount/1e18:.2f})")
                    self.execute_flashloan(amount, direction)
                    time.sleep(60)  # Wait after a trade
                else:
                    print("No profitable opportunity. Waiting...")
                    time.sleep(5)
            except Exception as e:
                print(f"Error in main loop: {e}")
                time.sleep(10)

# Flask app for Render port binding
app = Flask(__name__)

@app.route('/')
def index():
    return "Flash loan arbitrage bot is running."

def start_bot():
    bot = FlashLoanArbBot()
    bot.run()

if __name__ == "__main__":
    Thread(target=start_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
