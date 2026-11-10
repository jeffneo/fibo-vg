-- A small, made-up lake, written by Trino into Iceberg (Parquet files in the object store).
-- Every row carries `fibo_class`: the IRI of the FIBO class it is an instance of. That column is the join key between
-- the lake shard (Virtual Graph) and the ontology shard (FIBO in Neo4j) in the composite database.
CREATE SCHEMA IF NOT EXISTS iceberg.lake;

CREATE TABLE iceberg.lake.legal_entities (lei varchar, name varchar, jurisdiction varchar, fibo_class varchar);
INSERT INTO iceberg.lake.legal_entities VALUES
  ('EXAMPLE0000000000001', 'Harborline Bank N.A.',             'US-NY', 'https://spec.edmcouncil.org/fibo/ontology/FBC/FunctionalEntities/FinancialServicesEntities/CommercialBank'),
  ('EXAMPLE0000000000002', 'Harborline Securities LLC',        'US-DE', 'https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/FinancialProductsAndServices/BrokerDealer'),
  ('EXAMPLE0000000000003', 'Harborline Holdings Inc.',         'US-DE', 'https://spec.edmcouncil.org/fibo/ontology/BE/LegalEntities/CorporateBodies/StockCorporation'),
  ('EXAMPLE0000000000004', 'Northgate Growth Fund',            'US-MA', 'https://spec.edmcouncil.org/fibo/ontology/SEC/Funds/Funds/MutualFund'),
  ('EXAMPLE0000000000005', 'Northgate Total Market ETF',       'US-MA', 'https://spec.edmcouncil.org/fibo/ontology/SEC/Funds/Funds/ExchangeTradedFund'),
  ('EXAMPLE0000000000006', 'Kestrel Macro Partners LP',        'KY',    'https://spec.edmcouncil.org/fibo/ontology/SEC/Funds/Funds/HedgeFund'),
  ('EXAMPLE0000000000007', 'Kestrel Capital GP LLC',           'US-DE', 'https://spec.edmcouncil.org/fibo/ontology/BE/PrivateLimitedCompanies/PrivateLimitedCompanies/LimitedLiabilityCompany'),
  ('EXAMPLE0000000000008', 'State Teachers Retirement Plan',   'US-OH', 'https://spec.edmcouncil.org/fibo/ontology/SEC/Funds/Funds/PensionFund'),
  ('EXAMPLE0000000000009', 'Ashby Family Trust',               'US-CT', 'https://spec.edmcouncil.org/fibo/ontology/BE/Trusts/Trusts/Trust'),
  ('EXAMPLE0000000000010', 'Meridian Mutual Assurance',        'US-IL', 'https://spec.edmcouncil.org/fibo/ontology/FBC/FunctionalEntities/FinancialServicesEntities/InsuranceCompany'),
  ('EXAMPLE0000000000011', 'Copperline Manufacturing Co.',     'US-PA', 'https://spec.edmcouncil.org/fibo/ontology/BE/LegalEntities/CorporateBodies/PrivatelyHeldCompany'),
  ('EXAMPLE0000000000012', 'Lakeshore Community Credit Union', 'US-MI', 'https://spec.edmcouncil.org/fibo/ontology/FBC/FunctionalEntities/FinancialServicesEntities/CreditUnion');

-- who owns whom
CREATE TABLE iceberg.lake.ownership (owner_lei varchar, owned_lei varchar, pct double);
INSERT INTO iceberg.lake.ownership VALUES
  ('EXAMPLE0000000000003', 'EXAMPLE0000000000001', 100.0),
  ('EXAMPLE0000000000003', 'EXAMPLE0000000000002', 100.0),
  ('EXAMPLE0000000000007', 'EXAMPLE0000000000006', 1.0),
  ('EXAMPLE0000000000008', 'EXAMPLE0000000000006', 22.5),
  ('EXAMPLE0000000000010', 'EXAMPLE0000000000004', 8.0);

-- accounts: held by one entity, serviced (opened, kept) by another
CREATE TABLE iceberg.lake.accounts (account_id varchar, holder_lei varchar, servicer_lei varchar, product varchar, balance double, currency varchar, fibo_class varchar);
INSERT INTO iceberg.lake.accounts VALUES
  ('ACC-1001', 'EXAMPLE0000000000004', 'EXAMPLE0000000000001', 'custody',   412000000.0, 'USD', 'https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/ClientsAndAccounts/InvestmentAccount'),
  ('ACC-1002', 'EXAMPLE0000000000005', 'EXAMPLE0000000000001', 'custody',   958000000.0, 'USD', 'https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/ClientsAndAccounts/InvestmentAccount'),
  ('ACC-1003', 'EXAMPLE0000000000006', 'EXAMPLE0000000000002', 'prime',     187000000.0, 'USD', 'https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/ClientsAndAccounts/BrokerageAccount'),
  ('ACC-1004', 'EXAMPLE0000000000008', 'EXAMPLE0000000000001', 'custody',  2310000000.0, 'USD', 'https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/ClientsAndAccounts/InvestmentAccount'),
  ('ACC-1005', 'EXAMPLE0000000000008', 'EXAMPLE0000000000001', 'operating',   41000000.0, 'USD', 'https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/ClientsAndAccounts/DemandDepositAccount'),
  ('ACC-1006', 'EXAMPLE0000000000009', 'EXAMPLE0000000000002', 'brokerage',   12500000.0, 'USD', 'https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/ClientsAndAccounts/BrokerageAccount'),
  ('ACC-1007', 'EXAMPLE0000000000010', 'EXAMPLE0000000000001', 'custody',   640000000.0, 'USD', 'https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/ClientsAndAccounts/InvestmentAccount'),
  ('ACC-1008', 'EXAMPLE0000000000011', 'EXAMPLE0000000000001', 'operating',    3800000.0, 'USD', 'https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/ClientsAndAccounts/DemandDepositAccount'),
  ('ACC-1009', 'EXAMPLE0000000000011', 'EXAMPLE0000000000012', 'deposit',       950000.0, 'USD', 'https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/ClientsAndAccounts/DepositAccount'),
  ('ACC-1010', 'EXAMPLE0000000000006', 'EXAMPLE0000000000001', 'custody',    96000000.0, 'USD', 'https://spec.edmcouncil.org/fibo/ontology/FBC/ProductsAndServices/ClientsAndAccounts/InvestmentAccount');
