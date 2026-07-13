# SkySong Sales Analytics Dashboard

---

## 1. Executive Summary

The **SkySong Sales Analytics Dashboard** is a self-service reporting tool that turns raw event-booking spreadsheets into a clear, visual picture of SkySong's sales performance.

**The problem it solves:** Sales and booking data for SkySong events has historically lived in separate spreadsheets — one for net sales, one for gross sales, and one broken down by client host. Making sense of the full picture meant manually cross-referencing all three, which is slow, error-prone, and hard to repeat month over month.

**The solution:** This dashboard lets anyone upload those same three spreadsheets and, within seconds, see revenue trends, top clients, discount activity, and room usage all in one place — no spreadsheet formulas or manual cross-referencing required.

**Business value:**
- **Faster decisions** — leadership can see monthly and quarterly revenue trends at a glance instead of waiting on manual reports.
- **Better client insight** — instantly see which clients and client segments (ASU, Public, SkySong Tenants, etc.) generate the most revenue.
- **Transparency on discounts** — see exactly how much revenue is being given away in discounts, and to whom.
- **Reusable, every reporting period** — the same three files can be swapped out each month or quarter to refresh the whole dashboard.

---

## 2. Key Features & Benefits

Once your data is loaded, the dashboard organizes everything into easy-to-navigate tabs:

### 📈 Revenue Trends
- **Monthly Gross vs. Net Sales** chart, with discounts overlaid so you can see exactly how much revenue is lost to discounting each month.
- **Quarterly performance** comparison to track progress across the fiscal year.
- **Monthly booking volume** to see how busy each month was, separating paid bookings from internal (no-cost) ones.

### 🏢 Clients & Segments
- **Revenue by client segment** (ASU, Public, SkySong Tenants, Government, Education, etc.) shown as easy-to-read donut charts.
- **Top 15 clients by revenue**, ranked with visual bars so the biggest accounts are immediately obvious.
- **Client type breakdown** comparing total revenue against number of bookings per segment.

### ⚙️ Operations
- **Most-booked rooms**, so facilities and scheduling teams know which spaces are in highest demand.
- **Booking status** (Reserved vs. Tentative) at a glance.
- **Busiest days and times**, including a day-by-hour activity map to spot peak booking windows.
- **Typical event length** distribution across all bookings.

### 💰 Discounts
- **Discount totals by client segment**, so you can see which groups receive the most discounting.
- **Top 10 most-discounted clients**, with their gross, net, and discount amounts side by side.

### 📋 Bookings Table
- A **searchable, sortable, filterable** table of every individual booking — search by client, event, or room name, filter by segment or status, and sort by date or revenue.

### 🔍 Data Quality
- An automatic **data quality check** that flags anything unusual in the uploaded files (for example, mismatched totals or duplicate entries), so you can trust the numbers before sharing them.

### Snapshot Summary (Top of Every Dashboard)
At the very top of the dashboard, a row of summary cards always shows:
- Total Gross Sales & Total Net Sales
- Total Discounts given
- Total Events (and how many were paid vs. internal)
- Number of Unique Clients
- Average Revenue per Event
- Total Room Hours Booked
- Average Monthly Revenue

---

## 3. Step-by-Step User Guide

### How to Access the Dashboard

1. Open your web browser (Chrome, Edge, or Safari all work).
2. Go to: **https://skysong-dashboard.onrender.com**
3. You'll land on the **Upload** screen — this is where every session starts.

> **Note:** No login or password is required. Anyone with the link can open and use the dashboard.

### How to Upload Your Files

The dashboard needs **three specific files** to build the full report. It's smart enough to figure out which file is which — as long as the file name contains the right keyword.

| # | File | Filename Must Contain | Example |
|---|------|------------------------|---------|
| 1 | **Net Sales by Booking** | the word **"Net"** | `EMS_Net_Sales_by_Booking_FY26.xlsx` |
| 2 | **Gross Sales by Booking** | the word **"Gross"** | `Gross_Sales_by_Booking_FY26.xlsx` |
| 3 | **Gross Sales by Host** | the word **"Host"** | `Gross_Sales_by_Host_June_2026.xlsx` |

**Accepted file formats:** Excel files only — **.xlsx** or **.xls**. (CSV files are not currently supported.)

**Instructions:**

1. On the Upload screen, either:
   - **Drag and drop** all three files at once into the upload box, **or**
   - **Click the upload box** to browse your computer and select all three files together.
2. As each file is added, it appears in a checklist below the upload box with a colored tag confirming how it was identified (Net Sales, Gross Sales, or Host Report). Double-check that all three tags are correct and none show as "Unknown."
3. Once all three files are listed, click the gold **"Upload & Analyse"** button.
4. A progress bar will appear while the files are read — this usually takes just a few seconds.
5. You'll automatically be taken to the **Dashboard** page once the data has finished loading.

**Tip:** If you just want to see the dashboard in action without your own data, click **"⚡ Load Demo Data"** on the Upload screen to instantly preview the dashboard with sample files.

### Refreshing with New Data

To load a new reporting period at any time:
1. Click **"↑ Upload New"** in the top-right corner of the dashboard.
2. Repeat the upload steps above with your updated files.

### Switching Between Light and Dark Mode

Click the **🌙 / ☀️** icon in the top-right corner of the navigation bar at any time to switch the display between light and dark themes, depending on your preference.

---

*For questions about this dashboard or to request new features, please contact the project maintainer.*
