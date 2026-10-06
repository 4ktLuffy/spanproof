import sys
sys.argv=['repro_python.py','SP-01']
exec(compile(open('repro_python.py').read(),'repro_python.py','exec'))
