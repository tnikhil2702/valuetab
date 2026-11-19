import openpyxl
from datetime import datetime

from db import (
    AnalyticsReport,
    AnalyticsExecutiveSummary,
    AnalyticsWeeklyPerformance,
)

def open_excel(filepath):
    workbook = openpyxl.load_workbook(filepath, data_only=True)
    return workbook

def close_excel(workbook):
    workbook.close()

def find_header_row(ws, max_scan=20):

    for row in ws.iter_rows(min_row=1, max_row=max_scan):
        for cell in row:
            if cell.value == "MVA22_Signup":
                return cell.row

    raise ValueError(f"Header row not found in sheet: {ws.title}")


def parse_sheet_rows(ws, header_row):
    rows = []

    for row in ws.iter_rows(
        min_row=header_row + 1,
        max_col=11,
        values_only=False,  
    ):
        if row[0].value is None:
            continue

        rows.append(
            {
                "team":           row[0].value,
                "mva22_signup":   row[1].value or 0,
                "mva22_total":    row[2].value or 0,
                "mva22_rate":     row[3].value or 0,
                "total_edits":    row[4].value or 0,
                "total_created":  row[5].value or 0,
                "total_calls":    row[6].value or 0,
                "talk_time":      row[7].value,
                "overall_signup": row[8].value or 0,
                "overall_total":  row[9].value or 0,
                "overall_rate":   row[10].value or 0,
            }
        )

    return rows


def get_or_create_report(session, month, year):
    report = (
        session.query(AnalyticsReport)
        .filter_by(month=month, year=year)
        .first()
    )

    if report:
        return report

    report = AnalyticsReport(
        month=month,
        year=year,
        created_at=datetime.utcnow(),
    )

    session.add(report)
    session.flush()

    return report


def executive_summary(session, filepath, month, year):
    workbook = open_excel(filepath)

    try:
        if "Executive Summary" not in workbook.sheetnames:
            raise ValueError("Executive Summary sheet not found.")

        report = get_or_create_report(session, month, year)

        sheet = workbook["Executive Summary"]

        # Clear existing rows for this report before re-inserting
        session.query(AnalyticsExecutiveSummary).filter_by(
            report_id=report.id
        ).delete()

        header_row = find_header_row(sheet)
        rows = parse_sheet_rows(sheet, header_row)

        for row in rows:
            session.add(
                AnalyticsExecutiveSummary(
                    report_id=report.id,
                    **row,
                )
            )

        session.commit()

        return report

    finally:
        close_excel(workbook)


def weekly_performance(
    session,
    filepath,
    month,
    year,
    week_number,
    week_start=None,
    week_end=None,
):
    workbook = open_excel(filepath)

    try:
        if "Weekly Performance" not in workbook.sheetnames:
            raise ValueError("Weekly Performance sheet not found.")

        report = get_or_create_report(session, month, year)

        sheet = workbook["Weekly Performance"]

        # Clear existing rows for this report + week before re-inserting
        session.query(AnalyticsWeeklyPerformance).filter_by(
            report_id=report.id,
            week_number=week_number,
        ).delete()

        header_row = find_header_row(sheet)
        rows = parse_sheet_rows(sheet, header_row)

        for row in rows:
            session.add(
                AnalyticsWeeklyPerformance(
                    report_id=report.id,
                    week_number=week_number,
                    week_start=week_start,
                    week_end=week_end,
                    **row,
                )
            )

        session.commit()

        return report

    finally:
        close_excel(workbook)