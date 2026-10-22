import sys
import tkinter as tk

total = int(sys.argv[1]) if len(sys.argv) > 1 else 60

root = tk.Tk()
root.attributes("-topmost", True)
root.overrideredirect(True)
root.configure(bg="#1a1a1a")
width, height = 200, 60
root.geometry(f"{width}x{height}+{root.winfo_screenwidth() - width - 20}+20")
label = tk.Label(root, text="", font=("Consolas", 18, "bold"), fg="#00ff00", bg="#1a1a1a")
label.pack(expand=True)
remaining = total


def tick():
    global remaining
    if remaining > 0:
        h, m, s = remaining // 3600, remaining % 3600 // 60, remaining % 60
        label.config(text=f"{h:02d}:{m:02d}:{s:02d}")
        remaining -= 1
        root.after(1000, tick)
    else:
        label.config(text="ВРЕМЯ ВЫШЛО!", fg="#ff0000")
        root.after(5000, root.destroy)


tick()
root.mainloop()
