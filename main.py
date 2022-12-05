from shell import *
from files import load_files
from sim import Sim

if __name__ == "__main__":
    data_dict = load_files()
    shell = FactorioShell(data_dict)
    shell.cmdloop()
