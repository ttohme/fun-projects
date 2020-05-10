##Project : Savings Calculator
##Name: Tohme Tohme
##Create Date: 05/09/2020
##

import math
import os

print('Saving Calculator\n')

total = 0.00

start = float(input('enter the starting amount: $'))

print('\n')

rate_year = float(input('enter the rate: %'))

rate = rate_year / 100
print('\n')
choice = input("enter [Y] for yearly and [M] for monthly : ")
month_or_year = str.lower(choice)
print('\n')

total += start

ave = 0.00

m = 0

y = 0

i = 0

t = 0.0
monthly = 0.00
new = True
j = 0
nummonth = 0

while new == True:
    if month_or_year == "m":
        inputm = input('Enter this month savings or press [done] to end:  $')
        if inputm != "done":

            try:
                monthly = float(inputm)

            except ValueError:

                new = False
                exit(print("Wrong Input"))
        else:
            break
    elif month_or_year == "y":
        if nummonth == 0:
            nummonth = int(input("Number of Months"))

        if j == 0:
            inputm = input('Enter Monthly Savings $')
            try:
                monthly = float(inputm)
                j = j + 1
            except ValueError:
                print('wrong input')
                break
        elif j >= nummonth:
            break
        else:
            j = j + 1

    else:
        exit(print("Wrong Input"))

    total = round(total * (1.0 + (rate / 12.0)), 2)

    total += monthly
    total = round(total, 2)

    m = m + 1

    i = i + 1

    t = round(t + monthly, 2)

    ave = round(t / i, 2)

    if m == 12:
        m = 0

        y = y + 1

    print('\n after', y, ' years and ', m, ' months \n total: $', total, '\n')

print('\n total:$', total, ' interest: $', round(total - t - start, 2), ' ave monthly in: $', ave, '\n')
