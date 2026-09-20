class PositionSizer:
    def calculate(self,capital,risk_fraction,entry_price,stop_price):
        if entry_price==stop_price: raise ValueError('entry_price and stop_price cannot be equal')
        return capital*risk_fraction/abs(entry_price-stop_price)
