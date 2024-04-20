def get_stock_prices(symbol, **kwargs):
    """
    Fetch historical stock prices for a given symbol using Yahoo Finance.

    Args:
        symbol (str): The stock symbol to retrieve data for, e.g., 'AAPL' for Apple Inc.
        **kwargs: Additional keyword arguments to customize the query, such as 'start', 'end', 'period', etc.

    Returns:
        pandas.DataFrame: A DataFrame containing historical stock price data for the specified symbol.

    Note:
        - The 'symbol' parameter should be a valid stock symbol recognized by Yahoo Finance.
        - You can customize the query further by providing keyword arguments, such as 'start' and 'end' to specify a date range.
        - For a full list of available keyword arguments, refer to the Yahoo Finance API documentation.
    """
    from lionagi.libs import SysUtil
    SysUtil.check_import("yfinance")
    from yfinance import Ticker
    
    stock = Ticker(symbol)
    hist = stock.history(**kwargs)
    return hist