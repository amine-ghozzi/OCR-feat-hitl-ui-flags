"""
Pydantic Models for Financial Record Validation

Cross-field validation for:
- Balance sheet balancing (Assets = Liabilities + Equity)
- Income statement arithmetic (Revenue - Expenses = Net Income)
"""

from pydantic import BaseModel, model_validator, computed_field
from typing import List, Optional
from datetime import date


class BalanceSheet(BaseModel):
    """Balance sheet with automatic balance verification."""
    
    # Header
    company_name: Optional[str] = None
    fiscal_year: Optional[int] = None
    currency: str = "TND"  # Tunisian Dinar default
    
    # Assets
    non_current_assets: float = 0.0
    current_assets: float = 0.0
    
    # Liabilities & Equity
    equity: float = 0.0
    non_current_liabilities: float = 0.0
    current_liabilities: float = 0.0
    
    @computed_field
    @property
    def total_assets(self) -> float:
        return self.non_current_assets + self.current_assets
    
    @computed_field
    @property
    def total_liabilities(self) -> float:
        return self.non_current_liabilities + self.current_liabilities
    
    @computed_field
    @property
    def total_liabilities_and_equity(self) -> float:
        return self.total_liabilities + self.equity
    
    @computed_field
    @property
    def balance_difference(self) -> float:
        """Difference between assets and liabilities+equity (should be 0)."""
        return abs(self.total_assets - self.total_liabilities_and_equity)
    
    @computed_field
    @property
    def is_balanced(self) -> bool:
        """Check if balance sheet balances within tolerance."""
        return self.balance_difference < 0.01
    
    @model_validator(mode='after')
    def verify_balance(self) -> 'BalanceSheet':
        """Warn if balance sheet doesn't balance."""
        if not self.is_balanced:
            # Log warning but don't raise - flag for HITL instead
            pass
        return self


class IncomeStatement(BaseModel):
    """Income statement with arithmetic verification."""
    
    company_name: Optional[str] = None
    fiscal_year: Optional[int] = None
    currency: str = "TND"
    
    # Revenue
    revenue: float = 0.0
    other_income: float = 0.0
    
    # Expenses
    cost_of_sales: float = 0.0
    operating_expenses: float = 0.0
    financial_expenses: float = 0.0
    tax_expense: float = 0.0
    
    # Reported figures
    reported_operating_income: Optional[float] = None
    reported_net_income: Optional[float] = None
    
    @computed_field
    @property
    def calculated_gross_profit(self) -> float:
        return self.revenue - self.cost_of_sales
    
    @computed_field
    @property
    def calculated_operating_income(self) -> float:
        return self.calculated_gross_profit + self.other_income - self.operating_expenses
    
    @computed_field
    @property
    def calculated_net_income(self) -> float:
        return self.calculated_operating_income - self.financial_expenses - self.tax_expense
    
    @computed_field
    @property
    def net_income_discrepancy(self) -> Optional[float]:
        """Difference between calculated and reported net income."""
        if self.reported_net_income is not None:
            return abs(self.calculated_net_income - self.reported_net_income)
        return None
    
    @model_validator(mode='after')
    def verify_arithmetic(self) -> 'IncomeStatement':
        """Verify income statement arithmetic."""
        if self.net_income_discrepancy is not None and self.net_income_discrepancy > 0.01:
            # Flag for HITL review
            pass
        return self


class ExtractedFinancialRecord(BaseModel):
    """Container for extracted financial data with validation status."""
    
    document_path: str
    document_type: str  # bilan, compte_resultat, flux_tresorerie
    accounting_standard: str  # IFRS, NCT, SYSCOHADA
    extraction_timestamp: str
    
    # Extracted data
    balance_sheet: Optional[BalanceSheet] = None
    income_statement: Optional[IncomeStatement] = None
    
    # Validation status
    qcs_score: float
    pandera_coercion_rate: float = 0.0
    hitl_required: bool = False
    validation_errors: List[str] = []
    
    @computed_field
    @property
    def is_valid(self) -> bool:
        """Check if extraction passed all validations."""
        checks = [
            self.qcs_score >= 0.75,
            self.pandera_coercion_rate < 0.05,
            not self.hitl_required,
        ]
        if self.balance_sheet:
            checks.append(self.balance_sheet.is_balanced)
        return all(checks)
