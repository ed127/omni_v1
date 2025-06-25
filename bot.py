import os
import asyncio
import aiohttp
from web3 import Web3
from web3.exceptions import TransactionNotFound
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor
import time
import logging

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
        if not self.private_key:
            raise ValueError("PRIVATE_KEY environment variable not set.")
        self.account = self.web3.eth.account.from_key(self.private_key)
        self.address = self.account.address
        self.telegram_token = os.getenv("TELEGRAM_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.zerox_api_key = os.getenv("ZEROX_API_KEY")
        if not self.zerox_api_key:
            raise ValueError("ZEROX_API_KEY environment variable not set.")

        self.USDT = Web3.to_checksum_address("0x55d398326f99059fF775485246999027B3197955")
        self.BUSD = Web3.to_checksum_address("0xe9e7cea3dedca5984780Bafc599bD69aDd087D56")
        self.SLIPPAGE_BPS = 20  # 0.2% slippage
        self.AMOUNT_USDT = 95 * 10**18
        self.MIN_PROFIT = 0.3 * 10**18
        self.EXCLUDED_SOURCES = "PancakeSwap,MDEX"  # Unreliable sources
        self.AFFILIATE_ADDRESS = os.getenv("AFFILIATE_ADDRESS", "")

        self.session = None
        self.executor = ThreadPoolExecutor(max_workers=8)
        self.gas_strategy = "medium"
        self.bnb_price = None
        self.gas_price = None

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
                    print(f"Connected to BSC RPC: {rpc}")
                    return w3
            except Exception as e:
                print(f"Could not connect to {rpc}: {e}")
                continue
        raise Exception("No RPC connection available")

    async def _get_bnb_price(self):
        url = "https://bsc.api.0x.org/swap/v1/price"
        params = {
            "sellToken": "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            "buyToken": self.USDT,
            "sellAmount": str(10**18),
            "chainId": 56,
            "taker": self.address,
            "excludedSources": self.EXCLUDED_SOURCES,
            "intentOnFilling": "true"
        }
        headers = {"0x-api-key": self.zerox_api_key}
        try:
            async with self.session.get(url, params=params, headers=headers, timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return int(float(data['price']) * 1e18)
                elif resp.status == 429:
                    print("Rate limited - retrying after delay")
                    await asyncio.sleep(2)
                    return await self._get_bnb_price()
                else:
                    print(f"0x price API error: {resp.status} - {await resp.text()}")
                    return None
        except Exception as e:
            print(f"Error getting BNB price: {e}")
            return None

    async def _update_gas_parameters(self):
        try:
            self.bnb_price = await self._get_bnb_price() or self.bnb_price or 300 * 10**18
            current_gas = self.web3.eth.gas_price
            try:
                block = self.web3.eth.get_block('latest')
                base_fee = block.baseFeePerGas if 'baseFeePerGas' in block else current_gas
            except Exception as e:
                print(f"Could not get baseFeePerGas: {e}")
                base_fee = current_gas

            self.gas_price = min(int(current_gas * 1.15), int(base_fee * 1.5), 10 * 10**9)
            print(f"Gas Price: {self.gas_price / 1e9:.2f} Gwei")
        except Exception as e:
            print(f"Error updating gas: {e}")
            self.gas_price = self.web3.eth.gas_price

    async def _send_telegram(self, message):
        if not self.session or not self.telegram_token or not self.chat_id:
            print("Telegram not configured")
            return
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        data = {"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"}
        try:
            async with self.session.post(url, data=data, timeout=5) as resp:
                if resp.status != 200:
                    print(f"Telegram error: {await resp.text()}")
        except Exception as e:
            print(f"Telegram send error: {e}")

    async def _get_quote(self, sell_token, buy_token, sell_amount, retry=0):
        if not self.session:
            return None
        url = "https://bsc.api.0x.org/swap/v1/quote"
        params = {
            "sellToken": sell_token,
            "buyToken": buy_token,
            "sellAmount": str(sell_amount),
            "chainId": 56,
            "taker": self.address,
            "gasPrice": str(self.gas_price),
            "slippageBps": self.SLIPPAGE_BPS,
            "excludedSources": self.EXCLUDED_SOURCES,
            "intentOnFilling": "true",
            "affiliateAddress": self.AFFILIATE_ADDRESS
        }
        headers = {
            "0x-api-key": self.zerox_api_key,
            "0x-api-version": "1.0.0"
        }
        try:
            async with self.session.get(url, params=params, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 429 and retry < 3:
                    print(f"Rate limited (retry {retry+1}/3)")
                    await asyncio.sleep(1.5 * (retry+1))
                    return await self._get_quote(sell_token, buy_token, sell_amount, retry+1)
                elif resp.status == 403:
                    print(f"API key error: {await resp.text()}")
                else:
                    print(f"Quote error ({resp.status}): {await resp.text()}")
        except Exception as e:
            print(f"Quote exception: {e}")
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
            print(f"Swap execution failed: {e}")
            raise

    async def _wait_for_confirmation(self, tx_hash, timeout=45):
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                receipt = self.web3.eth.get_transaction_receipt(tx_hash)
                if receipt:
                    return receipt.status == 1
                await asyncio.sleep(1.5)
            except TransactionNotFound:
                await asyncio.sleep(2)
            except Exception as e:
                print(f"Confirmation error: {e}")
                await asyncio.sleep(2)
        print(f"Confirmation timeout: {tx_hash}")
        return False

    async def _calculate_net_profit(self, profit_wei, tx1_gas_used, tx2_gas_used):
        if not self.bnb_price:
            self.bnb_price = await self._get_bnb_price() or 300 * 10**18
        total_gas_wei = (tx1_gas_used + tx2_gas_used) * self.gas_price
        gas_cost_usdt_wei = (total_gas_wei * self.bnb_price) // (10**18)
        return profit_wei - gas_cost_usdt_wei

    async def _check_arbitrage(self):
        print("\nChecking arbitrage...")
        usdt_busd_task = self._get_quote(self.USDT, self.BUSD, self.AMOUNT_USDT)
        busd_usdt_task = self._get_quote(self.BUSD, self.USDT, self.AMOUNT_USDT)
        quote1, quote2 = await asyncio.gather(usdt_busd_task, busd_usdt_task)
        opportunities = []

        # USDT → BUSD → USDT path
        if quote1 and quote2:
            amount_out = int(quote1['buyAmount'])
            final_quote = await self._get_quote(self.BUSD, self.USDT, amount_out)
            if final_quote:
                final_amount = int(final_quote['buyAmount'])
                gross_profit = final_amount - self.AMOUNT_USDT
                net_profit = await self._calculate_net_profit(
                    gross_profit,
                    int(quote1['gas']),
                    int(final_quote['gas'])
                )
                profit_pct = (net_profit / self.AMOUNT_USDT) * 100
                opportunities.append({
                    "path": "USDT→BUSD→USDT",
                    "quote1": quote1,
                    "quote2": final_quote,
                    "net_profit": net_profit,
                    "profit_pct": profit_pct
                })

        # BUSD → USDT → BUSD path
        busd_balance = self._get_token_balance(self.BUSD)
        if quote2 and busd_balance >= self.AMOUNT_USDT:
            amount_out = int(quote2['buyAmount'])
            final_quote = await self._get_quote(self.USDT, self.BUSD, amount_out)
            if final_quote:
                final_amount = int(final_quote['buyAmount'])
                gross_profit = final_amount - self.AMOUNT_USDT
                net_profit = await self._calculate_net_profit(
                    gross_profit,
                    int(quote2['gas']),
                    int(final_quote['gas'])
                )
                profit_pct = (net_profit / self.AMOUNT_USDT) * 100
                opportunities.append({
                    "path": "BUSD→USDT→BUSD",
                    "quote1": quote2,
                    "quote2": final_quote,
                    "net_profit": net_profit,
                    "profit_pct": profit_pct
                })

        if not opportunities:
            print("No arbitrage found")
            return None, None, 0, None, 0

        best = max(opportunities, key=lambda x: x["net_profit"])
        return (
            best["quote1"],
            best["quote2"],
            best["net_profit"],
            best["path"],
            best["profit_pct"]
        )

    async def _execute_arbitrage(self, quote1, quote2, path, net_profit, profit_pct):
        try:
            await self._update_gas_parameters()
            await self._send_telegram(
                f"<b>🚀 Arbitrage Found</b>\n"
                f"Path: {path}\n"
                f"Profit: {net_profit/1e18:.6f} USDT\n"
                f"ROI: {profit_pct:.4f}%\n"
                f"Executing..."
            )

            tx1_hash = await asyncio.get_event_loop().run_in_executor(
                self.executor, self._execute_swap, quote1
            )
            if not await self._wait_for_confirmation(tx1_hash):
                await self._send_telegram("⚠️ TX1 failed")
                return False

            tx2_hash = await asyncio.get_event_loop().run_in_executor(
                self.executor, self._execute_swap, quote2
            )
            if not await self._wait_for_confirmation(tx2_hash):
                await self._send_telegram("⚠️ TX2 failed")
                return False

            await self._send_telegram(
                f"<b>✅ Arbitrage Complete</b>\n"
                f"Path: {path}\n"
                f"Net Profit: {net_profit/1e18:.6f} USDT\n"
                f"<a href='https://bscscan.com/tx/{tx1_hash}'>TX1</a> | "
                f"<a href='https://bscscan.com/tx/{tx2_hash}'>TX2</a>"
            )
            return True
        except Exception as e:
            await self._send_telegram(f"❌ Execution failed: {str(e)}")
            print(f"Arbitrage error: {e}")
            return False

    async def run(self):
        await self._send_telegram("🤖 Arbitrage Bot Started")
        async with aiohttp.ClientSession() as session:
            self.session = session
            self.bnb_price = await self._get_bnb_price() or 300 * 10**18
            while True:
                try:
                    # Update gas every 10 minutes
                    if int(time.time()) % 600 < 5:
                        await self._update_gas_parameters()

                    q1, q2, profit, path, pct = await self._check_arbitrage()
                    if profit > self.MIN_PROFIT:
                        print(f"Executing {path} (Profit: {profit/1e18:.6f} USDT)")
                        if await self._execute_arbitrage(q1, q2, path, profit, pct):
                            await asyncio.sleep(60)
                    else:
                        await asyncio.sleep(5)
                except Exception as e:
                    print(f"Main loop error: {e}")
                    await asyncio.sleep(10)

if __name__ == "__main__":
    bot = EnhancedArbitrageBot()
    asyncio.run(bot.run())
