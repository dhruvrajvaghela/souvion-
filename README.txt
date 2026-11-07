BOT MART - Discord Marketplace Bot
===================================

Files
-----
bot_mart.py        The whole bot (single file, Python + discord.py 2.x + SQLite)
requirements.txt   Dependencies
Procfile           Railway start command  ->  web: python bot_mart.py
.env.example       List of environment variables (no real secrets)
.gitignore         Keeps .env and database files out of Git


1. DISCORD DEVELOPER PORTAL
---------------------------
1. https://discord.com/developers/applications -> New Application -> "BOT MART".
2. Bot tab -> Reset Token -> copy it (this is BOT_TOKEN). Never share it.
3. Bot tab -> Privileged Gateway Intents -> turn ON:
      - SERVER MEMBERS INTENT      (welcome + auto-role)
      - MESSAGE CONTENT INTENT     (anti-spam repeated-message detection)
4. Invite the bot with this link (replace YOUR_APPLICATION_ID, the Application ID is on
   the General Information tab):

   https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&scope=bot+applications.commands&permissions=1099780189200

   That permission set is: View Channels, Manage Channels, Manage Roles, Manage Messages,
   Moderate Members, Send Messages, Embed Links, Attach Files, Read Message History.
   (Administrator is NOT requested.)
5. In your server: Server Settings -> Roles -> drag the BOT MART role ABOVE the roles it
   must manage (Customer, Verified Customer and any auto-role). Discord only lets a bot
   assign roles that are below its own highest role.
6. Your OWNER_ID: Discord Settings -> Advanced -> Developer Mode ON, then right-click your
   name -> Copy User ID.


2. RAILWAY DEPLOYMENT (exact steps)
-----------------------------------
A. Put the project on GitHub
   1. Create a new PRIVATE repository on GitHub.
   2. Upload bot_mart.py, requirements.txt, Procfile, README.txt, .env.example, .gitignore
      (do NOT upload any file containing your real token).

B. Create the Railway service
   1. https://railway.app -> New Project -> Deploy from GitHub repo -> pick your repo.
   2. Railway detects Python from requirements.txt and uses the Procfile
      (web: python bot_mart.py).

C. Add a persistent volume (so the SQLite database survives restarts and redeploys)
   1. Open the service -> add a Volume (the service's Volumes / "+ New" -> Volume option).
   2. Mount path:  /data
   3. Without a volume, your products, tickets and settings are wiped on every deploy.

D. Set the environment variables (service -> Variables)
      BOT_TOKEN                 your Discord bot token
      OWNER_ID                  your Discord user ID
      DATABASE_PATH             /data/bot_mart.db
      UPI_ID                    e.g. yourname@okhdfcbank
      UPI_QR_URL                (optional) https link to your UPI QR image. If empty, a QR with the
                                exact ticket amount is generated from UPI_ID automatically.
      RAZORPAY_KEY_ID           (optional) from Razorpay Dashboard -> Account & Settings -> API Keys
      RAZORPAY_KEY_SECRET       (optional) same page
      RAZORPAY_WEBHOOK_SECRET   (optional) any long random string you choose; reuse it in step F
   Railway injects PORT automatically; the bot listens on it for the Razorpay webhook.
   Leave the Razorpay variables empty to run UPI-only; the Razorpay buttons are then hidden.

E. Generate a public domain
   Service -> Settings -> Networking -> Generate Domain. Your webhook URL will be:
      https://<your-domain>.up.railway.app/razorpay/webhook
   (GET https://<your-domain>.up.railway.app/ answers "BOT MART is online" as a health check.)

F. Razorpay webhook (only for automatic Razorpay verification)
   1. Razorpay Dashboard -> Account & Settings -> Webhooks -> Add New Webhook.
   2. URL: the webhook URL from step E.
   3. Secret: exactly the value of RAZORPAY_WEBHOOK_SECRET.
   4. Active events: tick  payment_link.paid  (under Payment Link).
   5. Test with Razorpay TEST keys first, then switch to live keys.
   The bot verifies every webhook's HMAC-SHA256 signature, checks the paid amount against the
   ticket price, and only then delivers the repository link. Invalid signatures are rejected.
   Even without the webhook, the customer's "Check Razorpay Payment" button asks the Razorpay
   API directly whether the link has been paid.

G. Deploy and check
   Open the Deployments tab -> View Logs. You should see "Logged in as BOT MART" and
   "Synced N application commands". Slash commands can take a few minutes to appear the first time.


3. FIRST-TIME SETUP IN DISCORD
------------------------------
1. Run /martsetup  (server owner only). It creates, without duplicating anything:
     INFORMATION: welcome, rules
     BOT MART:    bot-catalog, premium-bots, prices
     SUPPORT:     purchase, reviews
     STAFF:       staff-chat, sales-logs, activity-logs
     Roles: Admin, Staff, Customer, Verified Customer
   Admin and Staff become authorised staff roles (they do NOT receive Administrator). Give the
   Staff role to the people who will verify payments. The store panel is posted in #purchase.
2. For each product run:  /setprice   and   /setrepo   (the bot ships with Souvion, Kiyomi and
   Sora 2.0, but a product only becomes purchasable once its price and repository link are set).
3. Optional: /editproduct to write the description and add an image URL.
4. /martpanel opens the dashboard for everything else (payments, staff, welcome, auto-role,
   moderation, logs, bot profile).
5. /sendpanel #channel posts the store panel in any other channel.


4. ADDING / CHANGING PRODUCTS WITHOUT TOUCHING THE CODE
------------------------------------------------------
All of this is owner-only and stored in the database:
  /addproduct name price description [image_url] [repo_url] [active]   add a new bot
  /editproduct product [new_name] [description] [image_url] [active]    edit (image_url "-" removes it)
  /setprice product price                                                change the INR price
  /setrepo product url                                                   change the private repo link
  /removeproduct product                                                 delete a product
  /products                                                              list everything
Or use /martpanel -> Product Management (buttons and modals for the same actions).
Catalogue channels (bot-catalog, premium-bots, prices) refresh automatically after changes.
Only the server owner (or OWNER_ID) can change repository links.


5. HOW A PURCHASE WORKS
-----------------------
1. Customer presses Purchase on the panel and picks a product from the dropdown.
2. The bot creates a private ticket (customer + staff only) showing price, description,
   payment instructions and the UPI QR. Only one open purchase ticket per customer.
3. Payment path A - UPI / Google Pay / PhonePe / QR:
   customer pays, presses "Submit Payment Reference", enters the UTR. Staff check their
   bank/UPI app, press "Verify Payment" and confirm. (Duplicate references are rejected.)
4. Payment path B - Razorpay: customer presses "Pay with Razorpay" and pays on Razorpay's
   checkout. Verification is automatic (webhook or "Check Razorpay Payment").
5. Only after verification the bot posts the private repository link in the ticket and DMs it,
   assigns the Customer and Verified Customer roles and writes to #sales-logs.
   The link is never shown before verification and is redacted from ticket transcripts.
6. "Close Ticket" saves a transcript to #activity-logs and deletes the channel.


6. COMMANDS
-----------
Owner:    /martsetup /martpanel /addproduct /editproduct /removeproduct /setprice /setrepo
          /products /staffrole /setavatar /setstatus /setactivity
Staff:    /martpanel (tickets, logs, help) /sendpanel /timeout /clear + ticket buttons
Everyone: /help and the panel buttons (Store, Purchase, Support, Help)
Bot bio and banner cannot be changed by bots through Discord's API, so they are not offered.


7. TROUBLESHOOTING
------------------
- Commands do not show up: wait a few minutes, make sure the invite included
  "applications.commands", then check the Railway logs for errors.
- "Could not create ticket channel": give the bot Manage Channels and make sure the SUPPORT
  category has room (50 channel limit per category).
- Roles are not assigned: move the BOT MART role above Customer / Verified Customer.
- Data disappears after redeploy: the Railway volume is missing or DATABASE_PATH is not
  /data/bot_mart.db.
- Razorpay webhook returns 400: the secret in Razorpay does not match RAZORPAY_WEBHOOK_SECRET.
- Razorpay webhook returns 503: RAZORPAY_WEBHOOK_SECRET is not set.


8. SECURITY NOTES
-----------------
- All secrets come from environment variables; none are hardcoded.
- Rotate BOT_TOKEN immediately if it is ever exposed.
- Owner-only: product commands, repository links, payment settings, staff roles, bot profile.
- Staff are limited to verifying payments, tickets, logs, moderation. Staff roles never get
  Administrator from this bot.
- A payment is marked verified exactly once (atomic database update), so a repeated webhook or
  double click cannot deliver twice.
- Auto-roles with admin/moderation permissions are refused.
