import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

import websockets
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import (ApplicationBuilder, CommandHandler, ContextTypes,
                          ConversationHandler, MessageHandler, filters)

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
APP_ID = os.getenv('DERIV_APP_ID')
DERIV_API_URL = "wss://ws.binaryws.com/websockets/v3"

# Conversation states
SELECTING_SYMBOL, SETTING_PRICE = range(2)

# Global variables
active_symbols = {}  # To store available synthetic indices
user_alerts = {}  # Format: {user_id: {symbol: {price: direction}}}
symbol_prices = {}  # Current prices of symbols
active_subscriptions = set()  # Currently subscribed symbols

async def connect_deriv():
    print("Connecting to Deriv API...")
    """Establish connection to Deriv API and maintain it."""
    while True:
        try:
            async with websockets.connect(DERIV_API_URL+'?app_id='+APP_ID) as websocket:
                logger.info("Connected to Deriv API")
                print("Connected to Deriv API")
                
                # Get active symbols first
                await get_active_symbols(websocket)
                
                # Main loop to handle subscriptions and price updates
                await handle_price_updates(websocket)
                
        except Exception as e:
            logger.error(f"Connection error: {e}")
            print(f"Connection error: {e}")
            await asyncio.sleep(5)  # Wait before reconnecting

async def get_active_symbols(websocket):
    """Fetch available synthetic indices."""
    request = {
        "active_symbols": "brief",
        "product_type": "basic"
    }
    await websocket.send(json.dumps(request))
    response = await websocket.recv()
    data = json.loads(response)
    
    if 'active_symbols' in data:
        for symbol in data['active_symbols']:
            if 'synthetic_index' in symbol['market']:
                symbol_id = symbol['symbol']
                display_name = symbol['display_name']
                active_symbols[symbol_id] = display_name
                logger.info(f"Added symbol: {display_name} ({symbol_id})")

async def handle_price_updates(websocket):
    """Subscribe to price updates for symbols and process them."""
    while True:
        # Check if we need to subscribe to any new symbols
        for symbol in active_subscriptions:
            if symbol not in symbol_prices:
                await subscribe_to_symbol(websocket, symbol)
        
        # Process incoming messages
        try:
            response = await asyncio.wait_for(websocket.recv(), timeout=30)
            data = json.loads(response)
            
            if 'tick' in data and 'symbol' in data['tick']:
                symbol = data['tick']['symbol']
                price = data['tick']['quote']
                
                # Update price and check alerts
                old_price = symbol_prices.get(symbol, None)
                symbol_prices[symbol] = price
                
                if old_price is not None:
                    await check_price_alerts(symbol, old_price, price)
                    
        except asyncio.TimeoutError:
            # Send a ping to keep the connection alive
            await websocket.send(json.dumps({"ping": 1}))
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            # If there's an issue, try to reconnect
            break

async def subscribe_to_symbol(websocket, symbol):
    """Subscribe to price updates for a specific symbol."""
    request = {
        "ticks": symbol,
        "subscribe": 1
    }
    await websocket.send(json.dumps(request))
    logger.info(f"Subscribed to {symbol}")

async def check_price_alerts(symbol, old_price, new_price):
    """Check if any price alerts should be triggered."""
    for user_id, alerts in user_alerts.items():
        if symbol in alerts:
            for alert_price, direction in alerts[symbol].items():
                alert_price = float(alert_price)
                
                if direction == "above" and old_price < alert_price <= new_price:
                    await send_alert(user_id, symbol, alert_price, new_price, "risen above")
                    # Remove one-time alerts
                    del alerts[symbol][alert_price]
                    
                elif direction == "below" and old_price > alert_price >= new_price:
                    await send_alert(user_id, symbol, alert_price, new_price, "fallen below")
                    # Remove one-time alerts
                    del alerts[symbol][alert_price]

async def send_alert(user_id, symbol, alert_price, current_price, direction_text):
    """Send alert notification to user."""
    display_name = active_symbols.get(symbol, symbol)
    message = f"🚨 PRICE ALERT 🚨\n\n{display_name} has {direction_text} {alert_price}\nCurrent price: {current_price}"
    
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(chat_id=user_id, text=message)
    logger.info(f"Alert sent to {user_id} for {symbol}")

# Telegram bot command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message when /start command is issued."""
    user_id = update.effective_user.id
    
    welcome_text = (
        "Welcome to Deriv Synthetic Indices Alert Bot!\n\n"
        "I can notify you when synthetic indices reach specific prices.\n\n"
        "Available commands:\n"
        "/setalert - Set a new price alert\n"
        "/myalerts - View your current alerts\n"
        "/deletealert - Delete a specific alert\n"
        "/deleteall - Delete all your alerts\n"
        "/help - Show this help message"
    )
    
    await update.message.reply_text(welcome_text)
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help message."""
    await start(update, context)
    return ConversationHandler.END

async def set_alert_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the alert setting process."""
    # Create a keyboard with available symbols
    symbol_list = "\n".join([f"{display} ({symbol})" for symbol, display in active_symbols.items()])
    
    await update.message.reply_text(
        "Please choose a synthetic index by typing its code (like 'R_10' or 'BOOM500'):\n\n"
        f"{symbol_list}"
    )
    
    return SELECTING_SYMBOL

async def symbol_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle symbol selection."""
    symbol = update.message.text.upper()
    
    if symbol not in active_symbols:
        await update.message.reply_text(
            "Invalid symbol. Please choose from the list or use /cancel to abort."
        )
        return SELECTING_SYMBOL
    
    context.user_data['selected_symbol'] = symbol
    
    await update.message.reply_text(
        f"You selected {active_symbols[symbol]} ({symbol}).\n\n"
        "Now, enter the price and direction in this format:\n"
        "PRICE DIRECTION\n\n"
        "For example:\n"
        "1234.5 above (to be notified when price rises above 1234.5)\n"
        "1234.5 below (to be notified when price falls below 1234.5)"
    )
    
    return SETTING_PRICE

async def price_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle price and direction setting."""
    user_id = update.effective_user.id
    symbol = context.user_data.get('selected_symbol')
    
    if not symbol:
        await update.message.reply_text("Something went wrong. Please try again with /setalert.")
        return ConversationHandler.END
    
    try:
        parts = update.message.text.lower().split()
        price = float(parts[0])
        direction = parts[1]
        
        if direction not in ["above", "below"]:
            raise ValueError("Direction must be 'above' or 'below'")
        
        # Initialize user alerts if needed
        if user_id not in user_alerts:
            user_alerts[user_id] = {}
        if symbol not in user_alerts[user_id]:
            user_alerts[user_id][symbol] = {}
        
        # Add the alert
        user_alerts[user_id][symbol][price] = direction
        
        # Add symbol to active subscriptions if not already there
        active_subscriptions.add(symbol)
        
        await update.message.reply_text(
            f"Alert set for {active_symbols[symbol]} ({symbol}).\n"
            f"You will be notified when the price goes {direction} {price}."
        )
        
    except (ValueError, IndexError) as e:
        await update.message.reply_text(
            f"Invalid format: {e}\n"
            "Please use format: PRICE DIRECTION (e.g., '1234.5 above')"
        )
        return SETTING_PRICE
    
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the current operation."""
    await update.message.reply_text("Operation cancelled.")
    return ConversationHandler.END

async def my_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's active alerts."""
    user_id = update.effective_user.id
    
    if user_id not in user_alerts or not user_alerts[user_id]:
        await update.message.reply_text("You don't have any active alerts.")
        return
    
    message = "Your active alerts:\n\n"
    
    for symbol, alerts in user_alerts[user_id].items():
        display_name = active_symbols.get(symbol, symbol)
        message += f"📊 {display_name} ({symbol}):\n"
        
        for price, direction in alerts.items():
            message += f"  • {direction.capitalize()} {price}\n"
        
        message += "\n"
    
    await update.message.reply_text(message)

async def delete_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete a specific alert."""
    user_id = update.effective_user.id
    
    if user_id not in user_alerts or not user_alerts[user_id]:
        await update.message.reply_text("You don't have any active alerts to delete.")
        return
    
    # Show current alerts with numbers
    message = "Reply with the number of the alert you want to delete:\n\n"
    alert_list = []
    
    for symbol, alerts in user_alerts[user_id].items():
        display_name = active_symbols.get(symbol, symbol)
        
        for price, direction in alerts.items():
            alert_list.append((symbol, price, direction))
            index = len(alert_list)
            message += f"{index}. {display_name} ({symbol}) - {direction} {price}\n"
    
    context.user_data['alert_list'] = alert_list
    await update.message.reply_text(message)

async def process_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process alert deletion."""
    user_id = update.effective_user.id
    alert_list = context.user_data.get('alert_list', [])
    
    try:
        index = int(update.message.text) - 1
        
        if 0 <= index < len(alert_list):
            symbol, price, direction = alert_list[index]
            del user_alerts[user_id][symbol][float(price)]
            
            # Remove empty nested dictionaries
            if not user_alerts[user_id][symbol]:
                del user_alerts[user_id][symbol]
            
            await update.message.reply_text(f"Alert deleted successfully.")
        else:
            await update.message.reply_text("Invalid number. Please try again.")
    
    except ValueError:
        await update.message.reply_text("Please enter a valid number.")

async def delete_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete all alerts for a user."""
    user_id = update.effective_user.id
    
    if user_id in user_alerts:
        del user_alerts[user_id]
        await update.message.reply_text("All your alerts have been deleted.")
    else:
        await update.message.reply_text("You don't have any alerts to delete.")

def main():
    print("Starting the bot...")
    """Start the bot and setup handlers."""
    # Create application
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Add conversation handler for setting alerts
    set_alert_conv = ConversationHandler(
        entry_points=[CommandHandler('setalert', set_alert_start)],
        states={
            SELECTING_SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, symbol_selected)],
            SETTING_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, price_selected)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    print("Conversation handler added.")
    
    # Add handlers
    print("Adding command handlers...")
    application.add_handler(CommandHandler("start", start))
    print("Start command handler added.")
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(set_alert_conv)
    application.add_handler(CommandHandler("myalerts", my_alerts))
    application.add_handler(CommandHandler("deletealert", delete_alert))
    application.add_handler(CommandHandler("deleteall", delete_all))
    print("Command handlers added.")
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_delete))
    print("Handlers added.")
        # Start the Deriv API connection in a separate thread
    loop = asyncio.get_event_loop()
    loop.create_task(connect_deriv())

    # Start the bot
    application.run_polling()
    print("Bot started.")
    print("Bot is running...")

if __name__ == '__main__':
    main()