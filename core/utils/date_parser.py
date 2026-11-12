# utils/date_parser.py

import re
from datetime import datetime

import pandas as pd
from dateutil import parser


# A 4-digit year in [1200, 1700) is Jalali: no real order data has a
# Gregorian year that early, and current Jalali years (~1400) sit inside.
_JALALI_YEAR_MIN = 1200
_JALALI_YEAR_MAX = 1700

_DIGIT_TRANSLATION = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"
)

# 1402-08-12, 1402/8/12, 1402.08.12 (optionally followed by a time part)
_YMD_PATTERN = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")


def _jalali_ymd_to_iso(year, month, day):
    """Convert a Jalali y/m/d to a Gregorian YYYY-MM-DD string."""
    import jdatetime

    try:
        return jdatetime.date(year, month, day).togregorian().strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(
            f"Invalid Jalali date: {year:04d}-{month:02d}-{day:02d}"
        ) from exc


class FlexibleDateParser:
    """
    Parses dates from various formats into YYYY-MM-DD.

    Jalali (Persian) dates are converted to Gregorian: the DB stores
    Gregorian dates and the reports convert back to Jalali years.
    """

    @staticmethod
    def parse_date(date_value):
        """
        Parse a date from various formats and return YYYY-MM-DD string
        Handles: Excel serial numbers, strings, datetime objects, etc.
        """
        if pd.isna(date_value) or date_value == "":
            return None

        # If it's already a datetime object
        if isinstance(date_value, (datetime, pd.Timestamp)):
            if _JALALI_YEAR_MIN <= date_value.year < _JALALI_YEAR_MAX:
                # Excel cell holding a Jalali date as a (bogus) Gregorian one.
                return _jalali_ymd_to_iso(
                    date_value.year, date_value.month, date_value.day
                )
            return date_value.strftime("%Y-%m-%d")

        # If it's a number (Excel serial date)
        if isinstance(date_value, (int, float)):
            try:
                # Convert Excel serial date to datetime
                excel_date = pd.Timestamp.fromordinal(int(date_value) - 693594)
                return excel_date.strftime("%Y-%m-%d")
            except:
                pass

        # If it's a string, try multiple formats
        if isinstance(date_value, str):
            date_value = date_value.strip()

            # Jalali dates ("1402-08-12", "1402/8/12", Persian digits too).
            # Must run before dateutil, which would read 1402 as a
            # Gregorian year and silently store the wrong date.
            date_value = date_value.translate(_DIGIT_TRANSLATION)
            match = _YMD_PATTERN.match(date_value)
            if match:
                year, month, day = map(int, match.groups())
                if _JALALI_YEAR_MIN <= year < _JALALI_YEAR_MAX:
                    return _jalali_ymd_to_iso(year, month, day)

            # Try python-dateutil parser (handles many formats)
            try:
                parsed_date = parser.parse(date_value, fuzzy=True)
                return parsed_date.strftime("%Y-%m-%d")
            except:
                pass

            # Try common formats manually
            common_formats = [
                "%Y-%m-%d",  # 2024-08-12
                "%d/%m/%Y",  # 12/08/2024
                "%m/%d/%Y",  # 08/12/2024
                "%d-%m-%Y",  # 12-08-2024
                "%m-%d-%Y",  # 08-12-2024
                "%d.%m.%Y",  # 12.08.2024
                "%b %d, %Y",  # Jan 12, 2024
                "%d %b %Y",  # 12 Jan 2024
                "%B %d, %Y",  # January 12, 2024
                "%d %B %Y",  # 12 January 2024
                "%d %b, %Y",  # 12 Jan, 2024
                "%Y%m%d",  # 20240812
                "%d-%b-%Y",  # 12-Jan-2024
            ]

            for fmt in common_formats:
                try:
                    parsed_date = datetime.strptime(date_value, fmt)
                    return parsed_date.strftime("%Y-%m-%d")
                except ValueError:
                    continue

        # If all parsing fails, log and return None or raise exception
        raise ValueError(f"Unable to parse date: {date_value}")

    @staticmethod
    def parse_persian_date(persian_date_str):
        """
        Optional: Convert Persian/Jalali dates to Gregorian
        Requires: pip install jdatetime
        """
        try:
            import jdatetime

            # Parse Persian date
            if "-" in persian_date_str:
                parts = persian_date_str.split("-")
            elif "/" in persian_date_str:
                parts = persian_date_str.split("/")
            else:
                return None

            if len(parts) == 3:
                year, month, day = map(int, parts)
                gregorian_date = jdatetime.date(year, month, day).togregorian()
                return gregorian_date.strftime("%Y-%m-%d")
        except:
            pass
        return None

    @staticmethod
    def parse_series_to_dates(date_series):
        """
        Parse a pandas Series of dates and return a new Series with parsed dates
        """
        return date_series.apply(FlexibleDateParser.parse_date)
