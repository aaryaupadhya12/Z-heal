Take your local path and take in this format cd "C:\Users\Aarya-2\Documents\ADOG\MARLOW AI\CPHarn\Z-heal"

Get-ChildItem data\Region_2_Quantiles  | Select-Object -First 10
Get-ChildItem data\Region_2_Cold_starts | Select-Object -First 5
Get-ChildItem data\Region2_part1        | Select-Object -First 5

Then 

Get-ChildItem data\Region_2_Quantiles\R2 | Select-Object -First 10
Get-ChildItem data\Region_2_Cold_starts\R2 | Select-Object -First 5
Get-ChildItem data\Region2_part1\R2_00000_00019 | Select-Object -First 5

Check then - easy wau to know if the dataset download is correct 


python pre_process\Huaweii_preprocess.py --root data\layout --region R2 --days 3 --requests-glob "data\Region2_part1\R2_00000_00019\*.csv"

@ Full run after the precheck
python pre_process\Huaweii_preprocess.py --root data\layout --region R2 --days all --requests-glob "data\Region2_part1\R2_00000_00019\*.csv"