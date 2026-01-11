from django.test import TestCase

# Create your tests here.
import pyodbc

conn = pyodbc.connect(
    "DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost\\MSSQLSERVER01;DATABASE=SmartLab;UID=sa;PWD=2580"
)
print("Connected!")
