import smbus2
from time import sleep

ADDRESS = 0x27 
LCD_CLEARDISPLAY = 0x01
LCD_RETURNHOME = 0x02
LCD_ENTRYMODESET = 0x04
LCD_DISPLAYCONTROL = 0x08
LCD_FUNCTIONSET = 0x20
LCD_SETDDRAMADDR = 0x80
LCD_DISPLAYON = 0x04
LCD_BACKLIGHT = 0x08
En = 0b00000100 
Rs = 0b00000001 

class LCD:
    def __init__(self, addr=ADDRESS):
        self.bus = smbus2.SMBus(1)
        self.addr = addr
        sleep(0.1)
        self.lcd_write(0x03)
        sleep(0.005)
        self.lcd_write(0x03)
        sleep(0.005)
        self.lcd_write(0x03)
        self.lcd_write(0x02)
        self.lcd_write(LCD_FUNCTIONSET | 0x08)
        self.lcd_write(LCD_DISPLAYCONTROL | LCD_DISPLAYON)
        self.lcd_write(LCD_CLEARDISPLAY)
        self.lcd_write(LCD_ENTRYMODESET | 0x02)
        sleep(0.2)

    def lcd_strobe(self, data):
        try:
            self.bus.write_byte(self.addr, data | En | LCD_BACKLIGHT)
            sleep(0.001) 
            self.bus.write_byte(self.addr, ((data & ~En) | LCD_BACKLIGHT))
            sleep(0.0005)
        except: pass

    def lcd_write_four_bits(self, data):
        try:
            self.bus.write_byte(self.addr, data | LCD_BACKLIGHT)
            self.lcd_strobe(data)
        except: pass

    def lcd_write(self, cmd, mode=0):
        self.lcd_write_four_bits(mode | (cmd & 0xF0))
        self.lcd_write_four_bits(mode | ((cmd << 4) & 0xF0))

    def display_string(self, string, line):
        if line == 1: self.lcd_write(0x80)
        if line == 2: self.lcd_write(0xC0)
        string = string.ljust(16)[:16]
        for char in string:
            self.lcd_write(ord(char), Rs)

    def clear(self):
        try:
            self.lcd_write(LCD_CLEARDISPLAY)
            sleep(0.005)
        except: pass