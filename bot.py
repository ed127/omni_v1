import os
import asyncio
import aiohttp
from web3 import Web3
from web3.exceptions import TransactionNotFound
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor
import time

load_dotenv()

class EnhancedArbitrageBot:
    def __init__(self):
        self.BSC_RPCS = [
            "https://bsc-dataseed.binance.org/",
            "https://bsc-dataseed1.defibit.io/",
            "https://bsc-dataseed1.ninicoin.io/"
        ]
        self.web3 = self._setup_web3()
        self.private_key = os.getenv("PRIVATE_KEY")
        self.account = self.web3.eth.account.from_key(self.private_key)
        self.address = self.account.address
        self.telegram_token = os.getenv("TELEGRAM_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        
        self.USDT = "0x55d398326f99059fF775485246999027B3197955"
        self.BUSD = "0xe9e7cea3dedca5984780bafc599bd69add087d56"
        self.SLIPPAGE = 0.002
        self.AMOUNT_USDT = 95 * 10**18
        self.MIN_PROFIT = 0.3 * 10**18
        
        self.session = None
        self.executor = ThreadPoolExecutor(max_workers=8)
        self.gas_strategy = "medium"
        self.bnb_price = None
        
        self.ERC20_ABI = [
            {
                "constant": True,
                "inputs": [{"name": "_owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "balance", "type": "uint256"}],
                "type": "function"
            }
        ]
    
    def _setup_web3(self):
        for rpc in self.BSC_RPCS:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 5}))
                if w3.is_connected():
                    return w3
            except:
                continue
        raise Exception("No RPC connection available")
    
    async def _get_bnb_price(self):
        url = "https://bsc.api.0x.org/swap/v1/price"
        params = {
            "sellToken": "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            "buyToken": self.USDT,
            "sellAmount": str(10**18)
        }
        try:
            async with self.session.get(url, params=params, timeout=2) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return int(float(data['price']) * 1e18)
        except:
            return None
    
    async def _update_gas_parameters(self):
        try:
            if not self.bnb_price:
                self.bnb_price = await self._get_bnb_price() or 300 * 10**18
            
            current_gas = self.web3.eth.gas_price
            try:
                base_fee = self.web3.eth.get_block('latest').baseFeePerGas
            except:
                base_fee = current_gas
            
            if self.gas_strategy == "aggressive":
                self.gas_price = min(int(current_gas * 1.3), int(base_fee * 2))
            else:
                self.gas_price = min(int(current_gas * 1.15), int(base_fee * 1.5))
                
        except:
            self.gas_price = self.web3.eth.gas_price
    
    async def _send_telegram(self, message):
        if not self.session:
            return
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        data = {"chat_id": self.chat_id, "text": message}
        try:
            async with self.session.post(url, data=data, timeout=3) as resp:
                pass
        except:
            pass
    
    async def _get_quote(self, sell_token, buy_token, sell_amount):
        if not self.session:
            return None
        
        url = "https://bsc.api.0x.org/swap/v1/quote"
        params = {
            "sellToken": sell_token,
            "buyToken": buy_token,
            "sellAmount": str(sell_amount),
            "slippagePercentage": self.SLIPPAGE,
            "takerAddress": self.address,
            "affiliateAddress": "0x0000000000000000000000000000000000000000"
        }
        
        try:
            async with self.session.get(url, params=params, timeout=3) as resp:
                if resp.status == 200:
                    return await resp.json()
        except:
            pass
        return None
    
    def _get_token_balance(self, token_address):
        contract = self.web3.eth.contract(
            address=token_address,
            abi=self.ERC20_ABI
        )
        return contract.functions.balanceOf(self.address).call()
    
    def _execute_swap(self, quote):
        try:
            txn = {
                'from': quote['from'],
                'to': quote['to'],
                'data': quote['data'],
                'value': int(quote['value']),
                'gas': min(int(quote['gas']) * 13 // 10, 800000),
                'gasPrice': self.gas_price,
                'nonce': self.web3.eth.get_transaction_count(self.address),
            }
            
            signed = self.web3.eth.account.sign_transaction(txn, self.private_key)
            tx_hash = self.web3.eth.send_raw_transaction(signed.rawTransaction)
            return self.web3.to_hex(tx_hash)
        except Exception as e:
            raise e
    
    async def _wait_for_confirmation(self, tx_hash, timeout=45):
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                receipt = self.web3.eth.get_transaction_receipt(tx_hash)
                if receipt is not None:
                    return receipt.status == 1
                await asyncio.sleep(1.5)
            except TransactionNotFound:
                await asyncio.sleep(2)
        return False
    
    async def _calculate_net_profit(self, profit, tx1_gas, tx2_gas):
        if not self.bnb_price:
            self.bnb_price = await self._get_bnb_price() or 300 * 10**18
        
        total_gas_wei = (tx1_gas + tx2_gas) * self.gas_price
        gas_cost_usdt = total_gas_wei * self.bnb_price // 10**18
        return profit - gas_cost_usdt
    
    async def _check_arbitrage(self):
        usdt_busd_task = self._get_quote(self.USDT, self.BUSD, self.AMOUNT_USDT)
        busd_usdt_task = self._get_quote(self.BUSD, self.USDT, self.AMOUNT_USDT)
        
        quote1, quote2 = await asyncio.gather(usdt_busd_task, busd_usdt_task)
        
        opportunities = []
        
        if quote1 and quote2:
            amount_out = int(quote1['buyAmount'])
            final_quote = await self._get_quote(self.BUSD, self.USDT, amount_out)
            if final_quote:
                final_amount = int(final_quote['buyAmount'])
                profit = final_amount - self.AMOUNT_USDT
                net_profit = await self._calculate_net_profit(
                    profit,
                    int(quote1['gas']),
                    int(final_quote['gas'])
                )
                if net_profit > self.MIN_PROFIT:
                    opportunities.append((
                        "USDT→BUSD→USDT", 
                        quote1, 
                        final_quote, 
                        net_profit
                    ))
        
        if quote2 and self._get_token_balance(self.BUSD) >= self.AMOUNT_USDT:
            amount_out = int(quote2['buyAmount'])
            final_quote = await self._get_quote(self.USDT, self.BUSD, amount_out)
            if final_quote:
                final_amount = int(final_quote['buyAmount'])
                profit = final_amount - self.AMOUNT_USDT
                net_profit = await self._calculate_net_profit(
                    profit,
                    int(quote2['gas']),
                    int(final_quote['gas'])
                )
                if net_profit > self.MIN_PROFIT:
                    opportunities.append((
                        "BUSD→USDT→BUSD", 
                        quote2, 
                        final_quote, 
                        net_profit
                    ))
        
        if not opportunities:
            return None, None, 0, None
        
        best_opp = max(opportunities, key=lambda x: x[3])
        return best_opp[1], best_opp[2], best_opp[3], best_opp[0]
    
    async def _execute_arbitrage(self, quote1, quote2, direction):
        try:
            await self._update_gas_parameters()
            
            tx1_hash = await asyncio.get_event_loop().run_in_executor(
                self.executor, self._execute_swap, quote1
            )
            
            if not await self._wait_for_confirmation(tx1_hash, 30):
                await self._send_telegram(f"⚠️ {direction} - TX1 failed: {tx1_hash}")
                return False
            
            tx2_hash = await asyncio.get_event_loop().run_in_executor(
                self.executor, self._execute_swap, quote2
            )
            
            tx1_link = f"https://bscscan.com/tx/{tx1_hash}"
            tx2_link = f"https://bscscan.com/tx/{tx2_hash}"
            
            await self._send_telegram(
                f"✅ {direction} executed:\nTX1: {tx1_link}\nTX2: {tx2_link}"
            )
            return True
            
        except Exception as e:
            await self._send_telegram(f"❌ {direction} execution failed: {str(e)}")
            return False
    
    async def run(self):
        await self._send_telegram("🚀 Enhanced arbitrage bot started")
        
        async with aiohttp.ClientSession() as session:
            self.session = session
            self.bnb_price = await self._get_bnb_price()
            
            while True:
                try:
                    await self._update_gas_parameters()
                    
                    quote1, quote2, net_profit, direction = await self._check_arbitrage()
                    
                    if net_profit > 0:
                        profit_usd = net_profit / 1e18
                        if net_profit > self.MIN_PROFIT:
                            print(f"Arbitrage: {direction} | Profit: {profit_usd:.6f} USDT")
                            
                            await self._send_telegram(
                                f"🎯 Opportunity: {direction}\nProfit: {profit_usd:.6f} USDT"
                            )
                            
                            if await self._execute_arbitrage(quote1, quote2, direction):
                                await asyncio.sleep(60)
                        else:
                            print(f"Small profit: {direction} | {profit_usd:.6f} USDT")
                    
                    await asyncio.sleep(5)
                    
                except Exception as e:
                    print(f"Error: {e}")
                    await self._send_telegram(f"⚠️ Bot error: {str(e)}")
                    await asyncio.sleep(30)
