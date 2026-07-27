# Refactor Plan for /history Page Improvements

## Issues Identified

1. **Status Logic Issues**:
   - EXPIRED status shows positive PnL when it should be 0%
   - CLOSED_SL with positive PnL should be renamed to indicate trailing stop or breakeven

2. **Missing Data in Table**:
   - No exit_price column in history table
   - No Risk/Reward (R:R) ratio column

3. **Summary Cards Missing**:
   - No max drawdown metric
   - No detailed PnL columns

4. **Filter Limitations**:
   - Only Symbol and Status filters available
   - No date range filter (Today, Last 7 Days, This Month, Custom)

## Implementation Plan

### 1. Backend Changes (src/services/firebase.py)

#### A. Fix Status Logic Issues
Update `_compute_pnl_pct()` to handle status-specific logic:
- EXPIRED: PnL should be 0% (or rename to CLOSED_DURATION with closed_at timestamp)
- CLOSED_SL with positive PnL: Check if moved to breakeven (CLOSED_BEP) instead

#### B. Add Exit Price Calculation
Store actual exit price in Firestore when trades close:
- Create `exit_price` field in trades collection
- Update all closing statuses (CLOSED_SL, CLOSED_TP, CLOSED_BEP) to calculate and store exit_price
- Exit price should be the actual price where position was closed

#### C. Calculate Risk/Reward Ratios
Add R:R calculations:
- Planned R:R = (TP - Entry) / (Entry - SL) [Long] or (Entry - TP) / (SL - Entry) [Short]
- Actual R:R = (Exit - Entry) / (Entry - SL) [Long] or (Entry - Exit) / (SL - Entry) [Short]

#### D. Add Max Drawdown to Summary
Update `get_trade_summary()` to calculate and return:
- max_drawdown_percentage: Maximum drawdown across all closed trades

### 2. Backend Changes (src/main.py)

Update `/api/trades` endpoint to:
- Add date range filter parameters (date_from, date_to)
- Pass through to `get_all_trades()`

Update `/api/summary` endpoint to:
- Return max_drawdown_percentage

### 3. Frontend Changes (src/static/history.js)

#### A. Add New Columns
- Exit Price: Display actual exit price from API response
- R:R Planned: Show calculated planned ratio (TP/SL distance)
- R:R Actual: Show calculated actual ratio (Exit/Entry distance vs SL/Entry distance)

#### B. Update Summary Cards
- Add Max Drawdown card to summary grid

#### C. Add Date Range Filter
- Add dropdown for date range filter (Today, Last 7 Days, This Month, Custom)
- Implement date parsing and filtering

#### D. Fix Status Display Logic
- Handle EXPIRED status with correct PnL (0% or closed logic)
- Handle CLOSED_SL with positive PnL (trailing stop)

### 4. Frontend Changes (src/static/history.html)

#### A. Update HTML Structure
- Add Exit Price column to table header
- Add R:R Planned and R:R Actual column headers
- Add new date range filter dropdown in filters section

#### B. Update Summary Grid
- Add max drawdown card to summary section

## Detailed Implementation Steps

### Step 1: Backend Logic Fixes

1. **Update Trade Closing Status** (`src/services/firebase.py:update_trade_status`):
   - Add exit_price field in update_trade_status when status in (CLOSED_SL, CLOSED_TP, CLOSED_BEP)

2. **Fix PnL Calculation Logic** (`src/services/firebase.py:_compute_pnl_pct`):
   - Modify to check status before calculating PnL from prices
   - EXPIRED should return 0% PnL or handle as CANCELED_TIME
   - CLOSED_SL with positive PnL: Check if it was from breakeven movement

3. **Add Exit Price Data**:
   - Store actual binance exit price when trades are closed via monitor.py
   - Calculate and store exit_price in Firestore for all CLOSED statuses

4. **Add R:R Calculations**:
   ```python
   def _calculate_rr_planned(trade):
       # Calculate planned (TP - Entry) / (Entry - SL) or similar
       pass

   def _calculate_rr_actual(trade):
       # Calculate actual (Exit - Entry) / (Entry - SL) or similar
       pass
   ```

5. **Update Summary Calculation** (`src/services/firebase.py:get_trade_summary`):
   - Add max_drawdown_percentage calculation
   - Implement drawdown calculation: (peak_balance - current_balance) / peak_balance * 100

### Step 2: API Endpoint Updates

1. **Update /api/trades** (`src/main.py`):
   - Add date_from and date_to query parameters
   - Pass to get_all_trades()
   - Add date filtering in firebase.py:get_all_trades()

2. **Update /api/summary** (`src/main.py`):
   - Ensure max_drawdown_percentage is returned

### Step 3: Frontend Updates

1. **Update history.html**:
   - Add Exit Price column to table
   - Add R:R Planned and R:R Actual columns
   - Add date range filter section

2. **Update history.js**:
   - Update applyFilters() to handle date range
   - Add column rendering for Exit Price and R:R
   - Update loadSummary() to include max_drawdown card
   - Add formatting functions for R:R ratios

### Step 4: Testing

1. Verify that EXPIRED trades show 0% PnL
2. Verify CLOSED_SL with positive PnL shows correct status
3. Verify exit_price is displayed in table
4. Verify R:R ratios are calculated and displayed
5. Verify max drawdown is shown in summary
6. Verify date range filtering works

## Risk Mitigation

1. **Data Integrity**:
   - Add data migrations for existing trades
   - Maintain backward compatibility for existing API clients

2. **Performance**:
   - Add index for trade dates in Firestore if needed
   - Optimize query performance for date range filtering

3. **Frontend**:
   - Ensure date parsing handles different timezone formats
   - Add loading states for date range filtering
