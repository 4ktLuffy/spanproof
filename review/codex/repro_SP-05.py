import sys
sys.argv=['repro_python.py','SP-05']
exec(compile(open('repro_python.py').read(),'repro_python.py','exec'))
