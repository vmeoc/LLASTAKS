"""
Generate clean banking PDFs with proper numerical data for RAG testing.
This replaces the problematic PDFs with encoding issues.
"""

import os
import random
from datetime import datetime, timedelta
from typing import List, Dict, Any
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.units import inch

class BankStatementGenerator:
    """Generate realistic banking PDFs with proper text extraction."""
    
    def __init__(self):
        self.styles = getSampleStyleSheet()
        self.companies = [
            "ACME Corp", "TechStart Ltd", "Global Solutions", "DataFlow Inc",
            "CloudTech", "InnovateLab", "FutureSoft", "NextGen Systems"
        ]
        self.merchants = [
            "Amazon", "Carrefour", "Shell Station", "McDonald's", "Uber",
            "Netflix", "Spotify", "EDF Energy", "Orange Telecom", "SNCF"
        ]
    
    def generate_transactions(self, start_date: datetime, num_transactions: int = 15) -> List[Dict[str, Any]]:
        """Generate realistic banking transactions with proper balance calculation."""
        # Start with a realistic opening balance
        opening_balance = random.uniform(3000, 6000)
        current_balance = opening_balance
        
        transactions = []
        current_date = start_date
        
        # Generate transactions in strict chronological order
        for i in range(num_transactions):
            # Advance date by 1-3 days for realistic spacing
            days_advance = random.randint(1, 3)
            current_date = current_date + timedelta(days=days_advance)
            
            # Don't go beyond the month
            if current_date.month != start_date.month:
                current_date = start_date.replace(day=28)  # Stay in same month
            
            # Determine transaction type
            is_credit = random.choice([True, False, False, False])  # 25% credit, 75% debit
            
            if is_credit:
                # Credits: salary, refunds, transfers
                if random.random() < 0.3:  # Salary
                    amount = random.uniform(2500, 4500)
                    description = f"Salary - {random.choice(self.companies)}"
                else:  # Other credits
                    amount = random.uniform(50, 500)
                    description = f"Transfer from {random.choice(['John Smith', 'Marie Dubois', 'Paul Martin'])}"
                
                # Apply credit to balance
                current_balance += amount
                debit_str = ""
                credit_str = f"{amount:.2f}"
            else:
                # Debits: purchases, bills, withdrawals
                amount = random.uniform(10, 300)
                description = f"Purchase - {random.choice(self.merchants)}"
                
                # Apply debit to balance
                current_balance -= amount
                debit_str = f"-{amount:.2f}"
                credit_str = ""
            
            # Create transaction record
            transactions.append({
                'date': current_date.strftime('%d/%m/%Y'),
                'description': description,
                'debit': debit_str,
                'credit': credit_str,
                'balance': f"{current_balance:.2f}"
            })
        
        return transactions
    
    def create_statement_pdf(self, output_path: str, month: int, year: int):
        """Create a banking statement PDF for a specific month/year."""
        doc = SimpleDocTemplate(output_path, pagesize=A4)
        story = []
        
        # Header
        header_style = ParagraphStyle(
            'CustomHeader',
            parent=self.styles['Heading1'],
            fontSize=16,
            spaceAfter=30,
            alignment=1  # Center
        )
        
        story.append(Paragraph("BANK STATEMENT", header_style))
        story.append(Paragraph("CLEAN BANK", self.styles['Heading2']))
        story.append(Spacer(1, 20))
        
        # Account info
        account_info = [
            ["Account Holder:", "John Doe"],
            ["Account Number:", "GB29 CLEA 1234 5678 9012 34"],
            ["Sort Code:", "12-34-56"],
            ["Statement Period:", f"{month:02d}/{year}"],
            ["Statement Date:", datetime.now().strftime("%d/%m/%Y")]
        ]
        
        account_table = Table(account_info, colWidths=[2*inch, 3*inch])
        account_table.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        
        story.append(account_table)
        story.append(Spacer(1, 30))
        
        # Generate transactions
        start_date = datetime(year, month, 1)
        transactions = self.generate_transactions(start_date)
        
        # Account summary
        total_credits = sum(float(t['credit']) for t in transactions if t['credit'])
        total_debits = sum(abs(float(t['debit'])) for t in transactions if t['debit'])
        
        # Calculate opening balance: closing balance - net transactions
        closing_balance = float(transactions[-1]['balance'])
        net_change = total_credits - total_debits
        opening_balance = closing_balance - net_change
        
        summary_data = [
            ["Description", "Amount (EUR)"],
            ["Opening Balance", f"{opening_balance:.2f}"],
            ["Total Credits", f"{total_credits:.2f}"],
            ["Total Debits", f"{total_debits:.2f}"],
            ["Closing Balance", f"{closing_balance:.2f}"]
        ]
        
        summary_table = Table(summary_data, colWidths=[3*inch, 2*inch])
        summary_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('GRID', (0, 0), (-1, -1), 1, colors.black)
        ]))
        
        story.append(Paragraph("ACCOUNT SUMMARY", self.styles['Heading3']))
        story.append(summary_table)
        story.append(Spacer(1, 30))
        
        # Transaction details
        story.append(Paragraph("TRANSACTION DETAILS", self.styles['Heading3']))
        
        # Transaction table header
        trans_data = [["Date", "Description", "Debit (EUR)", "Credit (EUR)", "Balance (EUR)"]]
        
        # Add transactions
        for trans in transactions:
            trans_data.append([
                trans['date'],
                trans['description'],
                trans['debit'],
                trans['credit'],
                trans['balance']
            ])
        
        trans_table = Table(trans_data, colWidths=[1*inch, 2.5*inch, 1*inch, 1*inch, 1*inch])
        trans_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('ALIGN', (2, 0), (-1, -1), 'RIGHT'),  # Right align amounts
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.lightgrey])
        ]))
        
        story.append(trans_table)
        
        # Build PDF
        doc.build(story)
        print(f"Generated: {output_path}")

def main():
    """Generate clean banking PDFs for testing."""
    generator = BankStatementGenerator()
    
    # Create output directory
    output_dir = "clean_pdfs"
    os.makedirs(output_dir, exist_ok=True)
    
    # Generate PDFs for different months
    months = [
        (1, 2024, "January"),
        (2, 2024, "February"), 
        (3, 2024, "March"),
        (4, 2024, "April"),
        (5, 2024, "May")
    ]
    
    for month, year, month_name in months:
        filename = f"clean_bank_statement_{year}_{month:02d}.pdf"
        output_path = os.path.join(output_dir, filename)
        generator.create_statement_pdf(output_path, month, year)
    
    print(f"\nGenerated {len(months)} clean banking PDFs in '{output_dir}/' directory")
    print("These PDFs have proper text extraction and numerical data for RAG testing.")

if __name__ == "__main__":
    main()
