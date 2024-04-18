import os
from ..utils.pd_util import to_pd_df

finnhub_key_scheme = "FINNHUB_API_KEY"


class FinnHub:
    """
    A utility class for interacting with the FinnHub API to retrieve financial and market data.

    Attributes:
        api_key (str): The API key used for authenticating with the FinnHub API.

    Methods:
        get_client(api_key=None):
            Returns a FinnHub API client instance for making API calls.

        get_info_df(info_kind="company_news", api_key=None, **kwargs):
            Retrieves data from the FinnHub API based on the specified 'info_kind' and returns it as a Pandas DataFrame.
    """

    api_key = os.getenv(finnhub_key_scheme)

    @classmethod
    def get_client(cls, api_key=None):
        """
        Creates and returns a FinnHub API client instance.

        Args:
            api_key (str): The API key used for authenticating with the FinnHub API. If not provided,
                the class-level API key (if set) will be used.

        Returns:
            finnhub.Client: A FinnHub API client instance.

        Raises:
            ImportError: If there is an error while importing the 'finnhub' library.
        """
        try:
            import finnhub

            return finnhub.Client(api_key=api_key or cls.api_key)
        except Exception as e:
            raise ImportError(f"Error occured during importing finnhub: {e}")

    @classmethod
    def get_info_df(cls, info_kind="company_news", api_key=None, **kwargs):
        """
        Retrieves data from the FinnHub API based on the specified 'info_kind' and returns it as a Pandas DataFrame.

        Args:
            info_kind (str): The type of information to retrieve from FinnHub. Default is "company_news".
            api_key (str): The API key used for authenticating with the FinnHub API. If not provided,
                the class-level API key (if set) will be used.
            **kwargs: Additional keyword arguments to customize the API query.

        Returns:
            pandas.DataFrame: A Pandas DataFrame containing the retrieved data.

        Raises:
            ValueError: If there is an error during the API call or while converting the data to a DataFrame.
        """
        client = cls.get_client(api_key)
        info_func = cls.get_finnhub_method(client, info_kind)
        try:
            results = info_func(**kwargs)
            try:
                df = to_pd_df(results)
                return df
            except Exception as e:
                raise ValueError(
                    f"Error occured during converting {info_kind} to DataFrame: {e}"
                )
        except Exception as e:
            raise ValueError(
                f"Error occured during getting {info_kind} from FinnHub: {e}"
            )

    @staticmethod
    def get_finnhub_method(client, info_kind):
        """
        Returns the appropriate FinnHub API method based on the specified 'info_kind'.

        Args:
            client (finnhub.Client): The FinnHub API client instance.
            info_kind (str): The type of information to retrieve from FinnHub.

        Returns:
            Callable or None: The corresponding FinnHub API method, or None if 'info_kind' is not recognized.
        """
        info_func = {
            "aggregate_indicator": client.aggregate_indicator,
            "company_basic_financials": client.company_basic_financials,
            "company_earnings": client.company_earnings,
            "company_eps_estimates": client.company_eps_estimates,
            "company_executive": client.company_executive,
            "company_news": client.company_news,
            "company_peers": client.company_peers,
            "company_profile": client.company_profile,
            "company_profile2": client.company_profile2,
            "company_revenue_estimates": client.company_revenue_estimates,
            "country": client.country,
            "crypto_exchanges": client.crypto_exchanges,
            "crypto_symbols": client.crypto_symbols,
            "economic_data": client.economic_data,
            "filings": client.filings,
            "financials": client.financials,
            "financials_reported": client.financials_reported,
            "forex_exchanges": client.forex_exchanges,
            "forex_rates": client.forex_rates,
            "forex_symbols": client.forex_symbols,
            "fund_ownership": client.fund_ownership,
            "general_news": client.general_news,
            "ownership": client.ownership,
            "ipo_calendar": client.ipo_calendar,
            "press_releases": client.press_releases,
            "news_sentiment": client.news_sentiment,
            "pattern_recognition": client.pattern_recognition,
            "price_target": client.price_target,
            "quote": client.quote,
            "recommendation_trends": client.recommendation_trends,
            "stock_dividends": client.stock_dividends,
            "stock_basic_dividends": client.stock_basic_dividends,
            "stock_symbols": client.stock_symbols,
            "transcripts": client.transcripts,
            "transcripts_list": client.transcripts_list,
            "earnings_calendar": client.earnings_calendar,
            "covid19": client.covid19,
            "upgrade_downgrade": client.upgrade_downgrade,
            "economic_code": client.economic_code,
            "calendar_economic": client.calendar_economic,
            "support_resistance": client.support_resistance,
            "technical_indicator": client.technical_indicator,
            "stock_splits": client.stock_splits,
            "forex_candles": client.forex_candles,
            "crypto_candles": client.crypto_candles,
            "stock_tick": client.stock_tick,
            "stock_nbbo": client.stock_nbbo,
            "indices_const": client.indices_const,
            "indices_hist_const": client.indices_hist_const,
            "etfs_profile": client.etfs_profile,
            "etfs_holdings": client.etfs_holdings,
            "etfs_sector_exp": client.etfs_sector_exp,
            "etfs_country_exp": client.etfs_country_exp,
            "international_filings": client.international_filings,
            "sec_sentiment_analysis": client.sec_sentiment_analysis,
            "sec_similarity_index": client.sec_similarity_index,
            "last_bid_ask": client.last_bid_ask,
            "fda_calendar": client.fda_calendar,
            "symbol_lookup": client.symbol_lookup,
            "stock_insider_transactions": client.stock_insider_transactions,
            "mutual_fund_profile": client.mutual_fund_profile,
            "mutual_fund_holdings": client.mutual_fund_holdings,
            "mutual_fund_sector_exp": client.mutual_fund_sector_exp,
            "mutual_fund_country_exp": client.mutual_fund_country_exp,
            "stock_revenue_breakdown": client.stock_revenue_breakdown,
            "stock_social_sentiment": client.stock_social_sentiment,
            "stock_investment_theme": client.stock_investment_theme,
            "stock_supply_chain": client.stock_supply_chain,
            "company_esg_score": client.company_esg_score,
            "company_earnings_quality_score": client.company_earnings_quality_score,
            "crypto_profile": client.crypto_profile,
            "company_ebitda_estimates": client.company_ebitda_estimates,
            "company_ebit_estimates": client.company_ebit_estimates,
            "stock_uspto_patent": client.stock_uspto_patent,
            "stock_visa_application": client.stock_visa_application,
            "stock_insider_sentiment": client.stock_insider_sentiment,
            "bond_profile": client.bond_profile,
            "bond_price": client.bond_price,
            "stock_lobbying": client.stock_lobbying,
            "stock_usa_spending": client.stock_usa_spending,
            "sector_metric": client.sector_metric,
            "mutual_fund_eet": client.mutual_fund_eet,
            "mutual_fund_eet_pai": client.mutual_fund_eet_pai,
            "isin_change": client.isin_change,
            "symbol_change": client.symbol_change,
            "institutional_profile": client.institutional_profile,
            "institutional_portfolio": client.institutional_portfolio,
            "institutional_ownership": client.institutional_ownership,
            "bond_yield_curve": client.bond_yield_curve,
            "bond_tick": client.bond_tick,
            "congressional_trading": client.congressional_trading,
            "price_metrics": client.price_metrics,
            "market_holiday": client.market_holiday,
            "market_status": client.market_status,
        }
        return info_func.get(info_kind, None)
