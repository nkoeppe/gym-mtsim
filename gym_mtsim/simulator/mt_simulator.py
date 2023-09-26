from typing import List, Tuple, Dict, Any, Optional

import os
try:
    import pickle5 as pickle
except ImportError:
    import pickle
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from ..metatrader import Timeframe, SymbolInfo, retrieve_data
from .order import OrderType, Order
from .exceptions import SymbolNotFound, OrderNotFound


class MtSimulator:

    def __init__(
            self, unit: str='USD', balance: float=10000., leverage: float=100.,
            stop_out_level: float=0.2, hedge: bool=True, symbols_filename: Optional[str]=None
        ) -> None:

        self.unit = unit
        self.balance = balance
        self.equity = balance
        self.margin = 0.
        self.leverage = leverage
        self.stop_out_level = stop_out_level
        self.hedge = hedge

        self.symbols_info: Dict[str, SymbolInfo] = {}
        self.symbols_data: Dict[str, pd.DataFrame] = {}
        self.orders: List[Order] = []
        self.closed_orders: List[Order] = []
        self.current_time: datetime = NotImplemented

        if symbols_filename:
            if not self.load_symbols(symbols_filename):
                raise FileNotFoundError(f"file '{symbols_filename}' not found")


    @property
    def free_margin(self) -> float:
        return self.equity - self.margin


    @property
    def margin_level(self) -> float:
        margin = round(self.margin, 6)
        if margin == 0.:
            return float('inf')
        return self.equity / margin


    def download_data(
            self, symbols: List[str], time_range: Tuple[datetime, datetime], timeframe: Timeframe
        ) -> None:
        from_dt, to_dt = time_range
        for symbol in symbols:
            si, df = retrieve_data(symbol, from_dt, to_dt, timeframe)
            self.symbols_info[symbol] = si
            self.symbols_data[symbol] = df


    def save_symbols(self, filename: str) -> None:
        with open(filename, 'wb') as file:
            pickle.dump((self.symbols_info, self.symbols_data), file)


    def load_symbols(self, filename: str) -> bool:
        if not os.path.exists(filename):
            return False
        with open(filename, 'rb') as file:
            self.symbols_info, self.symbols_data = pickle.load(file)
        return True

    def order_sl_or_tp_creator(self, order, low_or_high):
        if order.type == OrderType.Buy:
            if low_or_high=="Low":
                sl_or_tp = order.sl
            elif low_or_high=="High":
                sl_or_tp = order.tp
        elif order.type == OrderType.Sell:
            if low_or_high=="Low":
                sl_or_tp = order.tp
            elif low_or_high=="High":
                sl_or_tp = order.sl

        return sl_or_tp

    def sl_tp_conditions_creator(self, order, low_or_high):
        sl_or_tp = self.order_sl_or_tp_creator(order, low_or_high)

        if order.sl_tp_type == "pip":
            if low_or_high=="Low":
                return order.entry_price - sl_or_tp
            elif low_or_high=="High":
                return order.entry_price + sl_or_tp
        elif order.sl_tp_type == "percent":
            if low_or_high=="Low":
                return order.entry_price * (1 - sl_or_tp)
            elif low_or_high=="High":
                return order.entry_price * (1 + sl_or_tp)


    @staticmethod
    def check_is_not_none(condition):
        if condition is not None:
            return True
        else:
            return False


    def check_sl_tp_condition(self, order, current_ohlc):
        close_order = False
        sl_or_tp_low  = self.order_sl_or_tp_creator(order, low_or_high="Low")
        sl_or_tp_high = self.order_sl_or_tp_creator(order, low_or_high="High")


        if order.type == OrderType.Buy:
            if self.check_is_not_none(sl_or_tp_low):
                if current_ohlc["Low"] <= self.sl_tp_conditions_creator(order, "Low"):  # SL
                    close_order = True

            if self.check_is_not_none(sl_or_tp_high):
                if current_ohlc["High"] >= self.sl_tp_conditions_creator(order, "High"):  # TP
                    close_order = True

        if order.type == OrderType.Sell:
            if self.check_is_not_none(sl_or_tp_high):
                if current_ohlc["High"] >= self.sl_tp_conditions_creator(order, "High"):  # SL
                    close_order = True

            if self.check_is_not_none(sl_or_tp_low):
                if current_ohlc["Low"] <= self.sl_tp_conditions_creator(order, "Low"):  # TP
                    close_order = True

        if close_order:
            self.close_order(order)


    def tick(self, delta_time: timedelta=timedelta()) -> None:
        self._check_current_time()

        self.current_time += delta_time
        self.equity = self.balance

        for order in self.orders:
            order.exit_time = self.current_time
            current_ohlc = self.price_at(order.symbol, order.exit_time)
            order.exit_price = current_ohlc['Close']
            self._update_order_profit(order)
            self.equity += order.profit

            if self.check_is_not_none(order.sl_tp_type):
                self.check_sl_tp_condition(order, current_ohlc)


        while self.margin_level < self.stop_out_level and len(self.orders) > 0:
            most_unprofitable_order = min(self.orders, key=lambda order: order.profit)
            self.close_order(most_unprofitable_order)

        if self.balance < 0.:
            self.balance = 0.
            self.equity = self.balance


    def nearest_time(self, symbol: str, time: datetime) -> datetime:
        df = self.symbols_data[symbol]
        if time in df.index:
            return time
        try:
            i, = df.index.get_indexer([time], method='ffill')
        except KeyError:
            i, = df.index.get_indexer([time], method='bfill')
        return df.index[i]


    def price_at(self, symbol: str, time: datetime) -> pd.Series:
        df = self.symbols_data[symbol]
        time = self.nearest_time(symbol, time)
        return df.loc[time]


    def symbol_orders(self, symbol: str) -> List[Order]:
        symbol_orders = list(filter(
            lambda order: order.symbol == symbol, self.orders
        ))
        return symbol_orders


    def create_order(self, order_type: OrderType, symbol: str, volume: float, fee: float=0.0005, fee_type: str="fixed", sl: float=None, tp:float=None, sl_tp_type: str=None,) -> Order:
        self._check_current_time()
        self._check_volume(symbol, volume)
        if fee < 0.:
            raise ValueError(f"negative fee '{fee}'")

        if self.hedge:
            return self._create_hedged_order(order_type, symbol, volume, fee, fee_type, sl, tp, sl_tp_type)
        return self._create_unhedged_order(order_type, symbol, volume, fee, fee_type, sl, tp, sl_tp_type)


    def _create_hedged_order(self, order_type: OrderType, symbol: str, volume: float, fee: float, fee_type: str, sl: float, tp:float, sl_tp_type: str) -> Order:
        order_id = len(self.closed_orders) + len(self.orders) + 1
        entry_time = self.current_time
        entry_price = self.price_at(symbol, entry_time)['Close']
        exit_time = entry_time
        exit_price = entry_price

        order = Order(
            order_id, order_type, symbol, volume, fee,
            entry_time, entry_price, exit_time, exit_price, fee_type=fee_type, sl=sl, tp=tp, sl_tp_type=sl_tp_type,
        )
        self._update_order_profit(order)
        self._update_order_margin(order)

        if order.margin > self.free_margin + order.profit:
            raise ValueError(
                f"low free margin (order margin={order.margin}, order profit={order.profit}, "
                f"free margin={self.free_margin})"
            )

        self.equity += order.profit
        self.margin += order.margin
        self.orders.append(order)
        return order


    def _create_unhedged_order(self, order_type: OrderType, symbol: str, volume: float, fee: float, fee_type: str, sl: float, tp:float, sl_tp_type: str) -> Order:
        if symbol not in map(lambda order: order.symbol, self.orders):
            return self._create_hedged_order(order_type, symbol, volume, fee, fee_type, sl, tp, sl_tp_type)

        old_order: Order = self.symbol_orders(symbol)[0]

        if old_order.type == order_type:
            new_order = self._create_hedged_order(order_type, symbol, volume, fee, fee_type, sl, tp, sl_tp_type)
            self.orders.remove(new_order)

            entry_price_weighted_average = np.average(
                [old_order.entry_price, new_order.entry_price],
                weights=[old_order.volume, new_order.volume]
            )

            old_order.volume += new_order.volume
            old_order.profit += new_order.profit
            old_order.gross_profit += new_order.gross_profit
            old_order.margin += new_order.margin
            old_order.entry_price = entry_price_weighted_average
            old_order.fee = max(old_order.fee, new_order.fee)

            return old_order

        if volume >= old_order.volume:
             self.close_order(old_order)
             if volume > old_order.volume:
                 return self._create_hedged_order(order_type, symbol, volume - old_order.volume, fee, fee_type, sl, tp, sl_tp_type)
             return old_order

        partial_profit = (volume / old_order.volume) * old_order.profit
        partial_gross_profit = (volume / old_order.volume) * old_order.gross_profit
        partial_margin = (volume / old_order.volume) * old_order.margin

        old_order.volume -= volume
        old_order.profit -= partial_profit
        old_order.gross_profit -= partial_gross_profit
        old_order.margin -= partial_margin

        self.balance += partial_profit
        self.margin -= partial_margin

        return old_order


    def close_order(self, order: Order) -> float:
        self._check_current_time()
        if order not in self.orders:
            raise OrderNotFound("order not found in the order list")

        order.exit_time = self.current_time
        order.exit_price = self.price_at(order.symbol, order.exit_time)['Close']
        self._update_order_profit(order)

        self.balance += order.profit
        self.margin -= order.margin

        order.closed = True
        self.orders.remove(order)
        self.closed_orders.append(order)
        return order.profit


    def get_state(self) -> Dict[str, Any]:
        orders = []
        for order in reversed(self.closed_orders + self.orders):
            orders.append({
                'Id': order.id,
                'Symbol': order.symbol,
                'Type': order.type.name,
                'Volume': order.volume,
                'Entry Time': order.entry_time,
                'Entry Price': order.entry_price,
                'Exit Time': order.exit_time,
                'Exit Price': order.exit_price,
                'Profit': order.profit,
                'Gross Profit': order.gross_profit,
                'Margin': order.margin,
                'Fee': order.fee,
                'Fee Type': order.fee_type,
                'Closed': order.closed,
                'SL': order.sl,
                'TP': order.tp,
                'SL_TP_Type': order.sl_tp_type,
            })
        orders_df = pd.DataFrame(orders)

        return {
            'current_time': self.current_time,
            'balance': self.balance,
            'equity': self.equity,
            'margin': self.margin,
            'free_margin': self.free_margin,
            'margin_level': self.margin_level,
            'orders': orders_df,
        }


    def _update_order_profit(self, order: Order) -> None:
        diff = order.exit_price - order.entry_price
        v = order.volume * self.symbols_info[order.symbol].trade_contract_size
        local_gross_profit = v * (order.type.sign * diff)

        if order.fee_type=="fixed":
            local_profit = v * (order.type.sign * diff - order.fee)

        elif order.fee_type=="floating":
            local_profit = v * (order.type.sign * diff)
            if local_profit > 0:
                local_profit *= (1 - order.fee)
            else:
                local_profit *= (1 + order.fee)

        order.profit = local_profit * self._get_unit_ratio(order.symbol, order.exit_time)
        order.gross_profit = local_gross_profit * self._get_unit_ratio(order.symbol, order.exit_time)

        # print(f"fee profit: {local_profit}, no fee profit: {v * (order.type.sign * diff)}")
        # print(f"local_profit: {local_profit}, first element: {order.type.sign * diff}, fee prod: {(1 - order.fee)}")


    def _update_order_margin(self, order: Order) -> None:
        v = order.volume * self.symbols_info[order.symbol].trade_contract_size
        local_margin = (v * order.entry_price) / self.leverage
        local_margin *= self.symbols_info[order.symbol].margin_rate
        order.margin = local_margin * self._get_unit_ratio(order.symbol, order.entry_time)


    def _get_unit_ratio(self, symbol: str, time: datetime) -> float:
        symbol_info = self.symbols_info[symbol]
        if self.unit == symbol_info.currency_profit:
            return 1.

        if self.unit == symbol_info.currency_margin:
            return 1 / self.price_at(symbol, time)['Close']

        currency = symbol_info.currency_profit
        unit_symbol_info = self._get_unit_symbol_info(currency)
        if unit_symbol_info is None:
            raise SymbolNotFound(f"unit symbol for '{currency}' not found")

        unit_price = self.price_at(unit_symbol_info.name, time)['Close']
        if unit_symbol_info.currency_margin == self.unit:
            unit_price = 1. / unit_price

        return unit_price


    def _get_unit_symbol_info(self, currency: str) -> Optional[SymbolInfo]:  # Unit/Currency or Currency/Unit
        for info in self.symbols_info.values():
            if currency in info.currencies and self.unit in info.currencies:
                return info
        return None


    def _check_current_time(self) -> None:
        if self.current_time is NotImplemented:
            raise ValueError("'current_time' must have a value")


    def _check_volume(self, symbol: str, volume: float) -> None:
        symbol_info = self.symbols_info[symbol]
        if not (symbol_info.volume_min <= volume <= symbol_info.volume_max):
            raise ValueError(
                f"'volume' must be in range [{symbol_info.volume_min}, {symbol_info.volume_max}]"
            )
        if not round(volume / symbol_info.volume_step, 6).is_integer():
            raise ValueError(f"'volume' must be a multiple of {symbol_info.volume_step}")
