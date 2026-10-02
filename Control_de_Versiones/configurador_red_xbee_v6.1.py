#!/usr/bin/env python3
"""
Configurador local y remoto de redes XBee-PRO 900HP DigiMesh sin XCTU.

El programa se comunica con un XBee conectado a la Raspberry Pi mediante
un adaptador CP2102 USB-UART. Permite:

1. Leer la configuración actual del XBee local.
2. Configurar ID, AP, CE y NI.
3. Guardar los cambios en la memoria no volátil mediante WR.
4. Verificar que los valores fueron aplicados correctamente.
5. Descubrir los XBee de la misma red o de un rango de ID mediante ND.
6. Guardar un registro local de los módulos configurados.
7. Configurar ID, AP, CE y NI de un módulo remoto seleccionado por su MAC.

Organización de la comunicación:

    Lectura, configuración y verificación local: +++ y comandos AT de texto.
    Esto funciona con AP = 0, 1 o 2; entrar en modo comando no cambia AP.
    Para evitar que los datos recibidos por radio contaminen las respuestas AT,
    las sesiones locales utilizan AP = 0 de forma temporal y sin ejecutar WR.
    El AP original se conserva para mostrarlo y restaurarlo al terminar.
    Al iniciar, el XBee seleccionado guarda GT = 500 ms mediante WR.
    API se utiliza para ND, cambios temporales de red y comandos remotos.
    NO y NT también se consultan por texto antes de iniciar ND.

0x08 ejecuta comandos en el XBee local; 0x17/0x97 configura el remoto.
Los ID temporales del XBee local nunca se guardan con WR. Se restaura y
comprueba su ID al terminar, ante errores y al cancelar mediante Ctrl+C.
El barrido conserva HP, CM y el cifrado: solo encuentra radios compatibles.
El intervalo válido de ID del 900HP es 0000-7FFF, no 0000-FFFF.
Se conserva el descubrimiento API que ya funcionaba en este proyecto;
ATND por texto sería otra implementación posible, no incluida aquí.

Referencia del protocolo: manual Digi 90002173, firmware 900HP.
https://docs.digi.com/resources/documentation/digidocs/pdfs/90002173.pdf

Configuración recomendada para este proyecto:

    Raspberry: AP = 1, CE = 1  (Indirect Msg Coordinator)
    Sensores:  AP = 0, CE = 0  (Standard Router)

Requisito:

    python3 -m pip install pyserial
"""

import argparse
import json
import signal
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import serial
    from serial.tools import list_ports
except ModuleNotFoundError as error:
    if error.name == "serial":
        raise SystemExit(
            "Falta pyserial. Instálelo con: python3 -m pip install pyserial"
        ) from error

    raise


# ************************* CONFIGURACIÓN ************************* #

BAUDIOS = 9600
TIEMPO_ESPERA_SERIAL_S = 0.100

TIEMPO_RESPUESTA_AT_S = 1.0

TIEMPO_RESPUESTA_API_S = 2.0
TIEMPO_RESPUESTA_REMOTA_S = 10.0  # Espera por comando remoto; incluye el recorrido por radio.
ID_MAXIMO_900HP = 0x7FFF
INTENTOS_DESCUBRIMIENTO = 2  # Repite una vez si no llegó ningún nodo remoto.
MAX_LONGITUD_API = 2048  # Límite para las tramas usadas por este configurador.
TIEMPO_ENTRE_BYTES_API_S = 0.500  # Recupera una trama cortada sin consumir toda la espera ND.

TIEMPO_GUARDA_INICIAL_S = 1.100     # La configuración por defecto de los Xbees es de GT = 1s como tiempo de espera antes y despues de +++ para entrar a modo comando
GT_CONFIGURADOR_MS = 500
TIEMPO_GUARDA_S = 0.550


TIEMPO_MAXIMO_DESCUBRIMIENTO_S = 60.0

ZONA_HORARIA = ZoneInfo("America/Bogota")

RUTA_REGISTRO = Path(__file__).resolve().with_name(
    "xbee_configurados.json"
)

MODO_COMANDO = "comando"
MODO_API_1 = "api_1"
MODO_API_2 = "api_2"

NOMBRES_AP = {
    0: "Transparent Mode",
    1: "API Mode Without Escapes",
    2: "API Mode With Escapes",
}

NOMBRES_CE = {
    0: "Standard Router",
    1: "Indirect Msg Coordinator",
    2: "Non-Routing Module",
}

NOMBRES_TIPO_NODO = {
    0: "Coordinator",
    1: "Router",
    2: "End Device",
}

NOMBRES_ESTADO_AT = {
    0: "OK",
    1: "ERROR",
    2: "comando no válido",
    3: "parámetro no válido",
    4: "fallo de transmisión",
    12: "error de cifrado",
}

BAUDIOS_POR_BD = {
    0: 1200,
    1: 2400,
    2: 4800,
    3: 9600,
    4: 19200,
    5: 38400,
    6: 57600,
    7: 115200,
    8: 230400,
}

COMANDOS_LECTURA_OBLIGATORIOS = ("ID", "AP", "CE", "NI", "SH", "SL")
COMANDOS_LECTURA_ADICIONALES = ("VR", "BD", "HP", "CM", "DH", "DL")


# **************************** ERRORES ***************************** #

class ErrorXBee(Exception):
    """Error controlado durante la comunicación con el XBee."""


class ErrorComandoApi(ErrorXBee):
    """Conserva el estado API; None significa que no llegó la respuesta esperada."""

    def __init__(self, mensaje, estado=None):
        super().__init__(mensaje)
        self.estado = estado


# *********************** FUNCIONES BÁSICAS ********************** #

def fechaHoraActual():
    """Devuelve la fecha de Bogotá como texto, incluyendo segundos y zona horaria."""
    return datetime.now(ZONA_HORARIA).isoformat(timespec="seconds")


def bytesAEntero(datos):
    """Interpreta varios bytes como un entero; por ejemplo, b'\x00\x09' vale 9."""
    if not datos:
        return 0

    return int.from_bytes(datos, byteorder="big")


def descripcionModo(modo):
    """Describe el protocolo utilizado, sin confundir modo comando con AP=0."""
    if modo == MODO_COMANDO:
        return "modo comando AT local (independiente de AP)"

    if modo == MODO_API_1:
        return "API 1, sin escapes"

    return "API 2, con escapes"


def pedirConfirmacion(mensaje):
    """Repite la pregunta hasta recibir sí o no; devuelve True o False."""
    while True:
        respuesta = input(f"{mensaje} [s/n]: ").strip().lower()

        if respuesta in ("s", "si", "sí", "y", "yes"):
            return True

        if respuesta in ("n", "no"):
            return False

        print("Escriba s para sí o n para no.")


# ************************ PUERTO SERIAL ************************** #

def listarPuertosSeriales():
    return sorted(list_ports.comports(), key=lambda puerto: puerto.device) #Ordena los puertos encontrados usando puerto.device como criterio de ordenamiento 
 
"""
list_ports.comports() --> busca los puertos seriales que el sistema operativo detecta. Cada objeto tiene varias propiedades:

    puerto.device
    puerto.description
    puerto.manufacturer

Ejemplo: puerto1

    puerto1.device = "/dev/ttyUSB2"
    puerto1.description = "CP2102"
"""
"""
La función sorted() necesita saber con qué criterio ordenar, como list_ports.comports() devuelve objetos que representan puertos seriales, 
se utiliza cómo criterio de ordenamiento 'key' una función que toma un objeto puerto y devuelve el valor de la propiedad 'device' de ese objeto.

    lambda puerto: puerto.device  --->   def obtenerNombrePuerto(puerto):
                                             return puerto.device
    key=obtenerNombrePuerto

Ejemplo: 

    lambda puerto: puerto.device
    
    puerto = puerto1
    puerto1.device = "/dev/ttyUSB2"
    
    puerto = puerto2
    puerto2.device = "/dev/ttyUSB0"

    por lo tanto las claves son: 

    "/dev/ttyUSB2"
    "/dev/ttyUSB0"

    y sorted() las ordena de la siguiente manera: 

    "/dev/ttyUSB0"
    "/dev/ttyUSB2"

    Sin embargo, sorted() no devuelve la cadena "/dev/ttyUSB0" ni "/dev/ttyUSB2", sino que devuelve los objetos 
    completos puerto1 y puerto2, pero en el orden correcto según la propiedad 'device'. Es decir, devuelve algo así: 

    [puerto2, puerto1]
"""

def seleccionarPuerto(puertoPreferido=None, baudios=BAUDIOS):   # puertoPreferido=None, siginifica que, por defecto, no se le ha indicado qué puerto usar. Si quien llama a la función no me entrega un puerto, asumiré que no hay uno preseleccionado.

    if puertoPreferido:                                         # ¿Me dieron ya un puerto específico?

        """ seleccionarPuerto("/dev/ttyUSB0") 
            puertoPreferido = "/dev/ttyUSB0"
        """

        ni = identificarNiEnPuerto(puertoPreferido, baudios)    # Si sí, 'identificarNiEnPuerto' intenta comunicarse con el XBee conectado a ese puerto y averiguar su parámetro NI.
        return puertoPreferido, ni                              # ("/dev/ttyUSB0", "COORDINADOR") 

    puertos = listarPuertosSeriales()
    puertosIdentificados = []

    print()
    print("Puertos seriales disponibles:")

    if puertos:                                                 # si la lista de puertos no está vacía
        for indice, puerto in enumerate(puertos, start=1):      # start = 1 porque por defecto es 0

            """
            Supongamos que:

            puertos = [objetoPuertoUSB0, objetoPuertoUSB1]

            donde:

            objetoPuertoUSB0.device = "/dev/ttyUSB0"
            objetoPuertoUSB1.device = "/dev/ttyUSB1"

            Entonces:

            enumerate(puertos, start=1)

            genera en cada iteración:

            Primera iteración:
                indice = 1
                puerto = objetoPuertoUSB0

            Segunda iteración:
                indice = 2
                puerto = objetoPuertoUSB1
            """
            descripcion = puerto.description or "Sin descripción"  # Usa puerto.description si tiene contenido. Si está vacío, usa "Sin descripción". Ejemplo: puerto.description = "CP2102 USB to UART Bridge Controller"
            ni = identificarNiEnPuerto(puerto.device, baudios)     # Intenta descubrir el NI del XBee conectado a ese puerto.
            puertosIdentificados.append((puerto, ni))              # Hace una dupla de los objetos puerto y ni, y la agrega a la lista puertosIdentificados.

            """
            Ejemplo: 

            puertosIdentificados = [
                (objetoPuertoUSB0, "COORDINADOR"),
                (objetoPuertoUSB1, "ROUTER")
            ]
            """

            lineaPuerto = f"  {indice}. {puerto.device} - {descripcion}"    # 1. /dev/ttyUSB0 - CP2102 USB to UART

            if ni:
                lineaPuerto += f" | NI: {ni}"                               # 1. /dev/ttyUSB0 - CP2102 USB to UART | NI: COORDINADOR
            else:
                lineaPuerto += " | NI no disponible"                        # 1. /dev/ttyUSB0 - CP2102 USB to UART | NI no disponible

            print(lineaPuerto)

        print(f"  {len(puertos) + 1}. Escribir otra ruta")

        while True:
            respuesta = input("Seleccione el puerto: ").strip()

            try:
                opcion = int(respuesta)
            except ValueError:
                print("Debe escribir el número de una opción.")
                continue

            if 1 <= opcion <= len(puertos):                                 # Usuario escoge desde la opción 1 hasta la opción n, donde n = len(puertos)
                puerto, ni = puertosIdentificados[opcion - 1]               # Se resta 1 porque la lista puertosIdentificados empieza en índice 0, mientras que las opciones mostradas al usuario empiezan en 1.
                return puerto.device, ni                                    # /dev/ttyUSB0, "COORDINADOR"  o  /dev/ttyUSB1, "ROUTER"

            if opcion == len(puertos) + 1:                                  # Se escogió la opción de escribir otra ruta
                break

            print("Opción no válida.")

    else:                                                                   # si la lista de puertos SÍ está vacía
        print("  No se detectaron puertos automáticamente.")

    ruta = input("Ruta del puerto [/dev/ttyUSB0]: ").strip()                # Si se eligió Escribir otra ruta o no se detectó ningún puerto automáticamente, se le pide al usuario que escriba la ruta del puerto. 
    ruta = ruta or "/dev/ttyUSB0"                                           # Si el usuario no escribe nada, se usa "/dev/ttyUSB0" como valor predeterminado.       
    ni = identificarNiEnPuerto(ruta, baudios)
    return ruta, ni


def abrirPuerto(rutaPuerto, baudios):
    try:                                    # Try: "Intenta hacer lo siguiente, pero prepárate por si ocurre un error"
        return serial.Serial(               # Instrucción que abre el puerto serial
                      
            port=rutaPuerto,                # rutaPuerto = "/dev/ttyUSB0"

            baudrate=baudios,               # baudios = 9600 
            bytesize=serial.EIGHTBITS,      # 8 bits de datos 
            parity=serial.PARITY_NONE,      # Sin bit de paridad  
            stopbits=serial.STOPBITS_ONE,   # 1 bit de parada ----> se está configurando 9600 8N1

            timeout=TIEMPO_ESPERA_SERIAL_S, # cuánto tiempo esperará una operación de lectura antes de rendirse
            write_timeout=2.0,              # cuánto tiempo esperará una operación de escritura antes de rendirse
        )

    except serial.SerialException as error:
        raise ErrorXBee(
            f"no fue posible abrir {rutaPuerto}: {error}"
        ) from error


# *********************** MODO COMANDO AT ************************ #

def leerRespuestaTexto(puerto, tiempoEspera=TIEMPO_RESPUESTA_AT_S):
    # Si no se especifica tiempoEspera, se usa TIEMPO_RESPUESTA_AT_S.
    tiempoFinal = time.monotonic() + tiempoEspera  # Momento máximo hasta el cual se intentará completar la respuesta.
    respuesta = bytearray()                        # Almacena los bytes recibidos antes de encontrar \r o \n.
    recibioRespuesta = False

    while time.monotonic() < tiempoFinal:
        # Intenta leer un byte del puerto. Por ejemplo, b"O", b"K" o b"\r".
        # Si no llega ningún byte durante el timeout corto del puerto, devuelve b"".
        dato = puerto.read(1)

        if not dato:
            continue

        # \r y \n indican el final de una línea de respuesta.
        if dato in (b"\r", b"\n"):

            # Si llegó \r, se considera terminada la respuesta.
            # Si llegó \n, solo se termina si anteriormente se recibió contenido.
            if dato == b"\r" or recibioRespuesta:

                # Interpreta los bytes acumulados como caracteres ASCII y devuelve
                # un texto str. Ejemplo: bytearray(b"OK") se convierte en "OK".
                return respuesta.decode(
                    "ascii",
                    errors="replace",  # Reemplaza cualquier byte que no pueda interpretarse como ASCII.
                ).strip()             # Elimina espacios u otros caracteres en blanco de los extremos.

            # Ignora un \n recibido antes de que haya contenido en respuesta.
            continue

        # Añade a respuesta el único byte recibido.
        # Ejemplo: si respuesta contiene bytearray(b"O") y dato es b"K",
        # después de extend contiene bytearray(b"OK").
        respuesta.extend(dato)
        recibioRespuesta = True

    # No se recibió una línea completa antes de alcanzar tiempoFinal.
    return None

def entrarModoComando(puerto, tiempoGuarda=TIEMPO_GUARDA_S):
    puerto._bufferTramaApi = None  # La siguiente sesión API comienza con un lector nuevo.
    puerto.reset_input_buffer()     # borra todos los bytes que estuvieran pendientes de lectura.
    puerto.reset_output_buffer()    # borra datos pendientes de transmisión que todavía estuvieran en el buffer de salida

    time.sleep(tiempoGuarda) # Por defecto, el Xbee exige un tiempo de silencio de por lo menos 1 segundo antes y después de enviar +++
    puerto.write(b"+++")
    puerto.flush()              # Hace que Python espere hasta que los datos pendientes de escritura hayan sido enviados al sistema serial
    time.sleep(tiempoGuarda)

    tiempoFinal = time.monotonic() + TIEMPO_RESPUESTA_AT_S
    respuesta = leerRespuestaTexto(puerto, 0.6) # Se espera un máximo de 0.6 segundos para recibir la respuesta del XBee. Si no llega nada, se retorna None. Si llega algo, se retorna la respuesta decodificada y sin espacios ni saltos de línea al principio y al final.

    # Si había datos recibidos por radio, la primera línea puede no ser OK.
    # Se siguen leyendo líneas dentro del mismo límite hasta encontrarlo.
    while respuesta != "OK" and time.monotonic() < tiempoFinal:
        # Una trama binaria puede quedar pegada delante de OK antes del \r.
        if respuesta is not None and respuesta.endswith("OK"):
            respuesta = "OK"
            break

        tiempoRestante = tiempoFinal - time.monotonic()

        if tiempoRestante <= 0:
            break

        respuesta = leerRespuestaTexto(
            puerto,
            min(0.6, tiempoRestante),          # Usa el menor valor entre 0.6 segundos y el tiempo restante. Hace que cada lectura dure como máximo 0.6 segundos, pero que la última lectura no exceda el tiempo total restante.
        )

    if respuesta != "OK":                       # Según el manual, el equipo debe responder con OK\r una vez entre en el modo comnando
        raise ErrorXBee(
            "no fue posible entrar en modo comando. Compruebe TX/RX, GND, "
            "la alimentación de 3.3 V, el puerto y los baudios seleccionados. "
            "La secuencia +++ y los tiempos de guarda deben coincidir con CC/GT."
        )


def enviarComandoTexto(puerto, comando, parametro=None):   # Esta función envía comandos AT al XBee una vez que está en modo comando y recibe su respuesta
    texto = f"AT{comando}"                                 # Ejemplo: enviarComandoTexto(puerto, "NI") ---> ATNI\r

    if parametro is not None:

        parametroTexto = parametro if comando == "NI" else f"{parametro:X}"

        """
        if comando == "NI":
            parametroTexto = parametro
        else:
            parametroTexto = f"{parametro:X}" Si no es NI, se convierte el valor a hexadecimal en mayúsculas como se exige para los parámetros en el manual. Ejemplo: 9 ---> "9", 10 ---> "A", 15 ---> "F", 16 ---> "10"
        """

        texto += parametroTexto


    """
    Si se proporcionó un parámetro, lo añade al comando AT. Esto permite modificar el valor del registro.
    Ejemplo:

    texto = "ATNI"
    parametro = "COORDINADOR"
    resultado: ATNICOORDINADOR
    """

    puerto.reset_input_buffer()                # borra todos los bytes que estuvieran pendientes de lectura.
    puerto.write((texto + "\r").encode("ascii"))  # Se codifica, por ejemplo: ATNICOORDINADOR<CR> a bytes ascii
    puerto.flush()                             # Hace que Python espere hasta que los datos pendientes de escritura hayan sido enviados al sistema serial

    tiempoFinal = time.monotonic() + TIEMPO_RESPUESTA_AT_S
    respuesta = leerRespuestaTexto(puerto)    # retorna el arreglo de bytes que se encuentre en el buffer de entrada del puerto serial del XBee.
                                               # Se decodifica de bytes y sin espacios ni saltos de línea al principio y al final. Si no hay respuesta, retorna None.

    # Al escribir o ejecutar un comando de control se espera específicamente
    # OK. Las líneas ajenas se descartan mientras quede tiempo de respuesta.
    esperaOk = parametro is not None or comando in ("AC", "WR", "CN")

    while esperaOk and respuesta not in ("OK", "ERROR"):
        # Los bytes de una trama previa pueden quedar delante del OK real.
        if respuesta is not None and respuesta.endswith("OK"):
            respuesta = "OK"
            break

        if respuesta is not None and respuesta.endswith("ERROR"):
            respuesta = "ERROR"
            break

        tiempoRestante = tiempoFinal - time.monotonic()

        if tiempoRestante <= 0:
            break

        respuesta = leerRespuestaTexto(puerto, tiempoRestante)

    if respuesta == "ERROR" or respuesta is None:
        if respuesta == "ERROR":
            raise ErrorXBee(f"el XBee rechazó el comando {texto}")

        raise ErrorXBee(f"el XBee no respondió al comando {texto}")


    # Los comandos que modifican un registro (Por ejemplo, comandos en los que se les manda un parámetro para establecer su valor)
    # tienen que responder "OK" cuando se ejecutan correctamente.
    if parametro is not None:
        if respuesta != "OK":
            raise ErrorXBee(
                f"respuesta inesperada al configurar AT{comando}: {respuesta}"
            )

        return respuesta


    # Los comandos de control, como ATWR y ATCN, responden "OK" cuando se ejecutan correctamente.
    if comando in ("AC", "WR", "CN"):
        if respuesta != "OK":
            raise ErrorXBee(
                f"AT{comando} respondió: {respuesta}"
            )

        return respuesta


    # ATNI es la única consulta textual utilizada actualmente por el programa.
    # Puede devolver un nombre o una cadena vacía si todavía no se asignó un NI.
    if comando == "NI":
        return respuesta


    # Las demás consultas utilizadas por el programa devuelven números escritos como texto hexadecimal.
    # Ejemplo: ATID responde "0009" y se devuelve 9.

    if not respuesta:
        raise ErrorXBee(f"AT{comando} devolvió un valor numérico vacío")

    try:
        return int(respuesta, 16)  #  Convierte una cadena completa que representa un número hexadecimal en un entero

    except ValueError as error:
        raise ErrorXBee(
            f"AT{comando} no devolvió un hexadecimal válido: {respuesta!r}"
        ) from error


def entrarModoComandoAislado(puerto, tiempoGuarda=TIEMPO_GUARDA_S):
    """Entra con AP=0 temporal; recupera AP incluso si falla la propia entrada.

    El AP original se conserva antes de enviar AP=0. No se ejecuta WR.
    El aislamiento afecta a la salida UART durante el modo comando, no apaga RF.
    """
    apOriginal = None
    entradaConfirmada = False
    try:
        entrarModoComando(puerto, tiempoGuarda)
        entradaConfirmada = True
        # Se exigen dos lecturas válidas e iguales antes de tocar AP. Así una
        # línea aislada de los sensores no se toma como la configuración real.
        anterior = None
        for _ in range(4):
            try:
                apLeido = enviarComandoTexto(puerto, "AP")
            except ErrorXBee:
                anterior = None
                continue
            if apLeido in (0, 1, 2) and apLeido == anterior:
                apOriginal = apLeido
                break
            anterior = apLeido if apLeido in (0, 1, 2) else None
        if apOriginal is None:
            raise ErrorXBee("no fue posible confirmar el AP antes del aislamiento")

        # Se conserva también en el objeto puerto antes de la primera escritura.
        # Si se pierde el OK de AP0 o AC, la recuperación conoce el valor correcto.
        puerto._apRestaurarXbee = apOriginal
        if apOriginal != 0:
            # AP=0 se aplica únicamente en memoria mediante ATAC. No se utiliza
            # ATWR aquí, por lo que el AP original continúa guardado.
            enviarComandoTexto(puerto, "AP", 0)
            enviarComandoTexto(puerto, "AC")
            if enviarComandoTexto(puerto, "AP") != 0:
                raise ErrorXBee("no se confirmó el AP=0 temporal")
        puerto.reset_input_buffer()
        return apOriginal
    except BaseException as error:
        # Incluye Ctrl+C y fallos anteriores al try/finally del llamador.
        if entradaConfirmada:
            try:
                salirModoComando(puerto, apOriginal)
            except (ErrorXBee, serial.SerialException, OSError) as recuperacion:
                raise ErrorXBee(
                    f"Falló la entrada al modo aislado: {error}. "
                    f"También falló la recuperación: {recuperacion}"
                ) from error
        raise


def salirModoComando(puerto, apRestaurar=None):
    """Restaura AP, confirma ATCN y comprueba API; nunca oculta un fallo."""
    if apRestaurar is None:
        apRestaurar = getattr(puerto, "_apRestaurarXbee", None)
    ultimoError = None
    with protegerRestauracion():
        for intento in range(3):
            try:
                if intento:
                    # Un CN pudo ejecutarse aunque se perdiera su OK. Se vuelve
                    # a entrar; si aún seguía en modo comando, se limpia la línea.
                    try:
                        entrarModoComando(puerto, TIEMPO_GUARDA_INICIAL_S)
                    except ErrorXBee:
                        puerto.write(b"\r")
                        puerto.flush()
                        leerRespuestaTexto(puerto, 0.2)
                if apRestaurar is None:
                    apRestaurar = enviarComandoTexto(puerto, "AP")
                if apRestaurar not in (0, 1, 2):
                    raise ErrorXBee("no se conoce un AP válido para restaurar")

                # Restaura el modo serial que existía antes del AP=0 temporal.
                # No se usa WR: este cambio solo debe quedar activo en memoria.
                enviarComandoTexto(puerto, "AP", apRestaurar)
                if enviarComandoTexto(puerto, "AP") != apRestaurar:
                    raise ErrorXBee(f"no se confirmó ATAP={apRestaurar}")
                enviarComandoTexto(puerto, "CN") # ATCN\r ---> CN = Command Mode Exit. Se sale del modo comando y aplica a los cambios pendientes.

                if apRestaurar in (1, 2):
                    # La respuesta binaria prueba que el módulo volvió a API.
                    # Es una comprobación de la transición; las lecturas del
                    # menú continúan realizándose por comandos AT de texto.
                    modo = MODO_API_1 if apRestaurar == 1 else MODO_API_2
                    if enviarComandoApi(puerto, modo, "AP") != apRestaurar:
                        raise ErrorXBee("el AP activo no coincide con el restaurado")
                puerto._apRestaurarXbee = None
                return
            except (ErrorXBee, serial.SerialException, OSError) as error:
                ultimoError = error
    raise ErrorXBee(
        f"NO se pudo confirmar la restauración de AP={apRestaurar}. "
        f"No continúe suponiendo que el XBee volvió a su modo original. Causa: {ultimoError}"
    ) from ultimoError


def configurarGtAlIniciar(rutaPuerto, baudios):
    """Guarda GT=500 ms en el XBee seleccionado antes de mostrar el menú."""
    with abrirPuerto(rutaPuerto, baudios) as puerto:
        # La primera entrada conserva 1.1 s porque el módulo todavía puede
        # tener GT=1000 ms de fábrica. Después de WR se utilizarán 550 ms.
        apOriginal = entrarModoComandoAislado(
            puerto,
            TIEMPO_GUARDA_INICIAL_S,
        )
        configuracionTerminada = False

        try:
            gtActual = enviarComandoTexto(puerto, "GT")

            if gtActual != GT_CONFIGURADOR_MS:
                enviarComandoTexto(puerto, "GT", GT_CONFIGURADOR_MS)

            # AP=0 era solamente temporal. Se restaura antes de WR para que
            # la memoria no volátil conserve el AP real junto con GT=500 ms.
            enviarComandoTexto(puerto, "AP", apOriginal)

            # WR solo es necesario cuando GT todavía no era 500 ms. Esto evita
            # escribir repetidamente la memoria no volátil en cada arranque.
            if gtActual != GT_CONFIGURADOR_MS:
                enviarComandoTexto(puerto, "WR")

            configuracionTerminada = True
        finally:
            if configuracionTerminada:
                salirModoComando(puerto, apOriginal)
            else:
                salirModoComando(puerto, apOriginal)


# *********************** CONSULTAS AT LOCALES ******************** #

def identificarNiEnPuerto(rutaPuerto, baudios):
    """Lee ATNI sin modificar la configuración del XBee conectado."""

    try:
        with abrirPuerto(rutaPuerto, baudios) as puerto:
            apOriginal = entrarModoComandoAislado(
                puerto,
                TIEMPO_GUARDA_INICIAL_S,
            )

            try:
                ni = enviarComandoTexto(puerto, "NI").strip()
            finally:
                salirModoComando(puerto, apOriginal)

            return ni or None

    except (ErrorXBee, serial.SerialException, OSError, ValueError):
        # El puerto puede pertenecer a otro dispositivo o estar siendo usado.
        return None

    
def leerRegistrosConfiguracion(puerto):
    """Lee los registros locales dentro de una sesión de modo comando ya abierta."""
    configuracion = {}  # Crea un diccionario vacío

    # Un fallo en un registro obligatorio impide identificar/configurar el nodo.
    for comando in COMANDOS_LECTURA_OBLIGATORIOS:                            # COMANDOS_LECTURA_OBLIGATORIOS = ("ID", "AP", "CE", "NI", "SH", "SL")
        configuracion[comando] = enviarComandoTexto(puerto, comando)         # Ejemplo: configuracion["ID"] = enviarComandoTexto(puerto, "ID")

    
    for comando in COMANDOS_LECTURA_ADICIONALES:                             # COMANDOS_LECTURA_ADICIONALES = ("VR", "BD", "HP", "CM", "DH", "DL")
        try:
            configuracion[comando] = enviarComandoTexto(puerto, comando)
        except ErrorXBee:
            configuracion[comando] = None                                    # Algunos firmwares no ofrecen todos los registros adicionales. None significa 'no disponible'


    # SH y SL son dos mitades de la dirección. SH = Serial Number High, SL = Serial Number Low. 
    # Se combinan para formar la dirección MAC completa de 64 bits. Ejemplo: SH=0x0013A200, SL=0x40B9B5D2 ---> MAC=0013A20040B9B5D2
    configuracion["MAC"] = (
        f"{configuracion['SH']:08X}{configuracion['SL']:08X}"
    )

    """
    08X da ocho cifras hexadecimales, las dos mitades forman 16 cifras que es la dirección MAC completa. 
    0  → rellenar con ceros a la izquierda
    8  → ocupar exactamente 8 posiciones como mínimo
    X  → representar el número en hexadecimal, usando A-F mayúsculas
    """

    return configuracion


def leerConfiguracionLocal(puerto):
    """Entra con +++, lee todos los registros y sale con ATCN, incluso si falla."""
    apOriginal = entrarModoComandoAislado(puerto)

    try:
        configuracion = leerRegistrosConfiguracion(puerto)

        # ATAP devuelve 0 porque ese es el valor temporal de la sesión. Para
        # mostrar la configuración real se conserva el valor leído antes.
        configuracion["AP"] = apOriginal
        return configuracion
    finally:
        # finally también se ejecuta si una consulta genera ErrorXBee.
        salirModoComando(puerto, apOriginal)


def mostrarConfiguracion(configuracion):
    """Muestra los valores leídos; no envía comandos ni modifica el XBee."""
    ap = configuracion["AP"]
    ce = configuracion["CE"]

    print()
    print("Configuración actual del XBee")
    print("-" * 46)
    print(f"MAC SH:SL : {configuracion['MAC']}")
    print(f"NI        : {configuracion['NI'] or '(vacío)'}")
    print(f"ID        : 0x{configuracion['ID']:04X}")                       #04X es formato hexadecimal de 4 dígitos, rellenando con ceros a la izquierda si es necesario. Ejemplo: 9 ---> 0009
    print(f"AP        : {ap} - {NOMBRES_AP.get(ap, 'valor desconocido')}")  # Busca ap dentro del diccionario NOMBRES_AP (recordar que ahora ap = configuracion["AP"] y es un valor) 
    print(f"CE        : {ce} - {NOMBRES_CE.get(ce, 'valor desconocido')}")  # Si no existe, devuelve "valor desconocido"
    # AP describe la operación normal; Lectura describe cómo se consultó.
    # Así AP=1/2 y modo comando AT pueden aparecer juntos sin contradicción.
    # Las consultas locales usan modo comando; las remotas usan 0x17/0x97.
    lectura = configuracion.get("LECTURA", descripcionModo(MODO_COMANDO))
    print(f"Lectura   : {lectura}")

    if configuracion.get("BD") is not None:
        bd = configuracion["BD"]
        velocidad = BAUDIOS_POR_BD.get(bd)  # Ejemplo: BD=3 corresponde a 9600, según la tabla provista por el manual del fabricante.

        if velocidad:
            print(f"BD        : {bd:X} - {velocidad} baudios")
        else:
            print(f"BD        : 0x{bd:X}")  # Si no encontró correspondencia, al menos muestra el valor hexadecimal que leyó

    for comando in ("VR", "HP", "CM"):
        valor = configuracion.get(comando)

        if valor is not None:
            print(f"{comando:<10}: 0x{valor:X}")    # :<10 imprime el texto alineado a la izquirda ocupando 10 caracteres

    if configuracion.get("DH") is not None and configuracion.get("DL") is not None:
        print(
            "DH:DL     : "
            f"{configuracion['DH']:08X}{configuracion['DL']:08X}"
        )

    print("-" * 46)


# ******************** ENTRADA DE PARÁMETROS ********************* #

def pedirId(valorActual):
    """Solicita el ID hexadecimal; Enter conserva el valor actual."""
    while True:
        respuesta = input(
            f"ID de red en hexadecimal [0x{valorActual:04X}]: "
        ).strip()

        if not respuesta:
            return valorActual
        
        """
        if respuesta.lower().startswith("0x"):
            respuesta = respuesta[2:]  # Acepta tanto '0x0009' como '0009'. Si el usuario pone '0x0009' se pasa a '0009'
        """

        try:
            valor = int(respuesta, 16)                      # lo convierte en entero
        except ValueError:
            print("El ID debe ser un número hexadecimal.")
            continue

        if 0 <= valor <= 0x7FFF:  # Rango del firmware 900HP de este proyecto según el manual es de máximo 0x7FFF = 32767 en entero.
            return valor

        print("Para el XBee-PRO 900HP, ID debe estar entre 0x0000 y 0x7FFF.")  # Si no se ejecuta el return, no se sale de la funcion y se imprime el mensaje de error y vuelve a pedir el ID.


def pedirAp(valorActual):
    """ Pide el modo de operación serial que tendrá el XBee después de salir de modo comando.
        Solo cambia el formato usado durante la operación normal del módulo.
    """
    print()
    print("AP - Modo de operación serial")
    print("  0. Transparent Mode")
    print("  1. API Mode Without Escapes")
    print("  2. API Mode With Escapes")
    print("Recomendación: Coordinador = 1; Router = 0.")

    while True:
        respuesta = input(f"Seleccione AP [{valorActual}]: ").strip()

        if not respuesta:
            return valorActual

        if respuesta in ("0", "1", "2"):
            return int(respuesta)

        print("AP solamente puede ser 0, 1 o 2.")


def pedirCe(valorActual):
    """Conserva las opciones de rol utilizadas en el montaje: CE=0 o CE=1."""
    print()
    print("CE - Routing/Messaging Mode")
    print("  0. Standard Router")
    print("  1. Indirect Msg Coordinator")
    print("Recomendación: Coordinador = 1; Router = 0.")

    # Si el módulo tenía otro CE, se propone 0 dentro de las opciones del menú.
    valorPredeterminado = valorActual if valorActual in (0, 1) else 0

    while True:
        respuesta = input(f"Seleccione CE [{valorPredeterminado}]: ").strip()

        if not respuesta:
            return valorPredeterminado

        if respuesta in ("0", "1"):
            return int(respuesta)

        print("Con este configurador, CE solamente puede ser 0 o 1.")


def pedirNi(valorActual):
    """Pide un nombre ASCII de hasta 20 caracteres y evita separadores de comandos."""
    while True:
        respuesta = input(f"NI - Nombre del nodo [{valorActual}]: ").strip() # .strip() elimina espacios, tabuladores y saltos de línea al principio y al final.
                                                                             # "   ROUTER   ".strip() ---> "ROUTER"
        if not respuesta:
            respuesta = valorActual

        try:
            datos = respuesta.encode("ascii")                       # Aquí se intenta convertir el texto Python a bytes ASCII.
        except UnicodeEncodeError:
            print("NI solamente puede contener caracteres ASCII.")
            continue

        if not datos:
            print("NI no puede quedar vacío.")
            continue

        if len(datos) > 20:                                           # cada caracter ASCII ocupa un byte, por lo que len(datos) devuelve la cantidad de bytes que ocupa el nombre. Si es mayor a 20, se rechaza.
            print("NI puede tener como máximo 20 caracteres ASCII.")
            continue

        if "," in respuesta or "\r" in respuesta or "\n" in respuesta: # \r es Carriage Return y \n es Line Feed. Ambos tienen significado especial en protocolos de texto y terminación de comandos.
            print("NI no puede contener comas ni saltos de línea.")    # La coma se evita porque en modo comando AT puede usarse como separador entre comandos.
            continue

        # ASCII también contiene caracteres de control, como tabulador y ESC.
        # Para un nombre solo se admiten caracteres imprimibles.
        if any(byte < 0x20 or byte > 0x7E for byte in datos):                   # Los caracteres imprimibles normales están entre 0x20 y 0x7E
            print("NI solamente puede contener caracteres ASCII imprimibles.")
            continue

        return respuesta    # Si se llega a este punto, significa que el nombre ingresado es válido y se retorna.


def pedirNuevaConfiguracion(configuracionActual):
    """Reúne y muestra los cuatro valores; todavía no escribe nada en el XBee."""
    print()
    print("Introduzca la nueva configuración.")
    print("Presione Enter para conservar el valor mostrado entre corchetes.")
    print()

    nueva = {
        "ID": pedirId(configuracionActual["ID"]),
        "AP": pedirAp(configuracionActual["AP"]),
        "CE": pedirCe(configuracionActual["CE"]),
        "NI": pedirNi(configuracionActual["NI"]),
    }

    print()
    print("Configuración que se escribirá")
    print(f"  ID: 0x{nueva['ID']:04X}")
    print(f"  AP: {nueva['AP']} - {NOMBRES_AP[nueva['AP']]}")
    print(f"  CE: {nueva['CE']} - {NOMBRES_CE[nueva['CE']]}")
    print(f"  NI: {nueva['NI']}")

    return nueva


# ********************* ESCRITURA Y VERIFICACIÓN ***************** #

def configurarEnModoComando(puerto, nuevaConfiguracion):
    """Entra con +++, escribe los cuatro parámetros, guarda y sale con ATCN."""

    apOriginal = entrarModoComandoAislado(puerto)
    configuracionTerminada = False

    try:
        # AP se escribe después de los demás parámetros para conservar AP=0
        # temporal durante la mayor parte de la configuración.
        for comando in ("ID", "CE", "NI"):

            parametro = nuevaConfiguracion[comando]

            # Mediante parametro se está estableciendo un nuevo valor al registro del XBee. Ejemplo:enviarComandoTexto(puerto, "ID", "1A") ---> ATID1A\r
            # Cada vez que se le cambia un valor a un registro mediante un comando, el programa responde con OK. Si no responde OK, significa que hubo un problema.
            enviarComandoTexto(puerto, comando, parametro)

        enviarComandoTexto(puerto, "AP", nuevaConfiguracion["AP"])

        # WR escribe en memoria no volátil. Envía ATWR para conservar la configuración después de apagar el módulo.
        # Los comandos de control como ATWR también tienen que devolver OK.
        enviarComandoTexto(puerto, "WR")
        configuracionTerminada = True

        time.sleep(0.250)  # Se deja una pequeña pausa antes de transmitir el siguiente comando.

    finally:
        # Un error no debe dejar intencionalmente el módulo en modo comando. Esto no revierte escrituras parciales
        if configuracionTerminada:
            # AP ya contiene el valor definitivo escrito por el usuario.
            salirModoComando(puerto, nuevaConfiguracion["AP"])
        else:
            # Si la operación falla antes de WR, se recupera el AP anterior.
            salirModoComando(puerto, apOriginal)


def comprobarConfiguracion(configuracionLeida, configuracionEsperada):
    """Devuelve una lista de diferencias; una lista vacía significa coincidencia."""
    diferencias = []

    for comando in ("ID", "AP", "CE", "NI"):
        if configuracionLeida[comando] != configuracionEsperada[comando]:   
            diferencias.append(
                f"{comando}: esperado {configuracionEsperada[comando]!r}, " # !r Es especialmente útil para distinguir texto de números. Para valor = "COORDINADOR", f"{valor}" devuelve COORDINADOR, mientras que f"{valor!r}" devuelve 'COORDINADOR', incluyendo las comillas simples. Esto ayuda a ver si hay espacios o caracteres invisibles en el texto.
                f"leído {configuracionLeida[comando]!r}"                    # añade a la lista algo como "AP: esperado 1, leído 0". Entonces diferencias = ["AP: esperado 1, leído 0"]
            )

    return diferencias


# *************************** REGISTRO JSON**************************** #

def cargarRegistro():
    """Su propósito es obtener el contenido actual del JSON y devolverlo como un diccionario de Python"""

    if not RUTA_REGISTRO.exists():  # pregunta si el archivo existe. Si no existe, devuelve una estructura inicial del registro. Si por ejemplo registro = cargarRegistro(), y el archivo no existe, entonces registro = {"version": 1, "actualizado": None, "modulos": [], "ultimo_descubrimiento": None}
        return {
            "version": 1,
            "actualizado": None,
            "modulos": [],
            "ultimo_descubrimiento": None,
        }  


    try:    # with: cierra automáticamente el archivo, incluso si ocurre un error.
        with RUTA_REGISTRO.open("r", encoding="utf-8") as archivo:  # Si el archivo existe, RUTA_REGISTRO.open(...) abre en modo lectura "r" el archivo cuya ubicación está guardada en RUTA_REGISTRO. encoding="utf-8" indica cómo deben interpretarse los caracteres del archivo. "as archivo" guarda el archivo abierto en una variable llamada archivo
            registro = json.load(archivo)                           # json.load(archivo) lee el JSON y lo convierte en estructuras de Python

            """

            Por ejemplo, si el archivo contiene:

            {
            "version": 1,
            "modulos": []
            }

            registro = json.load(archivo)

            Produce: 

            registro = {
                "version": 1,
                "modulos": [],
            }

            """

    except (OSError, json.JSONDecodeError) as error:
        raise ErrorXBee(
            f"no fue posible leer {RUTA_REGISTRO.name}: {error}"
        ) from error

    # setdefault añade una clave solo si no existía. Conserva registros previos.
    registro.setdefault("version", 1)   
    registro.setdefault("actualizado", None)
    registro.setdefault("modulos", [])                  
    registro.setdefault("ultimo_descubrimiento", None)
    return registro


def escribirRegistro(registro):
    """Recibe un diccionario y lo guarda en xbee_configurados.json"""
    registro["actualizado"] = fechaHoraActual()
    rutaTemporal = RUTA_REGISTRO.with_suffix(".json.tmp")   # Crear una ruta temporal xbee_configurados.json.tmp

    try:
        with rutaTemporal.open("w", encoding="utf-8") as archivo:       # Escribir primero el archivo temporal
            json.dump(registro, archivo, indent=4, ensure_ascii=False)  # Escribe registro a archivo. json.dump() convierte el diccionario de Python en JSON
            archivo.write("\n")                                         # añade un salto de línea al final del archivo.

        rutaTemporal.replace(RUTA_REGISTRO)                             # Solo después de terminar correctamente la escritura se reemplaza el JSON anterior del arhivo termporal al archivo de la ruta verdadera.
    except OSError as error:
        raise ErrorXBee(
            f"no fue posible escribir {RUTA_REGISTRO.name}: {error}"
        ) from error


def guardarModuloEnRegistro(configuracion, rutaPuerto, macLocal=None):
    """Esta función añade un XBee nuevo o actualiza uno ya registrado, identificándolo por su MAC."""
    registro = cargarRegistro()
    mac = configuracion["MAC"]

    modulo = {
        "mac": mac,
        "ni": configuracion["NI"],
        "id": f"0x{configuracion['ID']:04X}",
        "ap": configuracion["AP"],
        "ce": configuracion["CE"],
        "funcion": NOMBRES_CE.get(
            configuracion["CE"],
            "Desconocida",
        ),
        "puerto_usado": rutaPuerto,
        "fecha_configuracion": fechaHoraActual(),
        "configurado_por": "remoto" if macLocal else "local",
        "via_mac": macLocal,
    }

    # La MAC no cambia si cambia el NI o Linux cambia el número ttyUSB.
    for indice, anterior in enumerate(registro["modulos"]):
        if anterior.get("mac") == mac:
            registro["modulos"][indice] = modulo
            break

    else:  # El else del for se ejecuta solo si no se encontró una MAC y no hubo break. ---> for ... else Ejecuta el else solamente si el for termina de recorrer todos sus elementos sin haber ejecutado un break.
        registro["modulos"].append(modulo) # La lista registro["modulos"] = [] pasa a [{"mac": "0013A20040B9B5D2", "ni": "COORDINADOR", "id": "0x0009", "ap": 1, "ce": 1, "funcion": "Indirect Msg Coordinator", "puerto_usado": "/dev/ttyUSB0", "fecha_configuracion": "2024-06-15 12:34:56"}]
                                           # De esta manera se crea el primer elemento de la lista registro["modulos"]


    """
    Supongamos que: 

    registro["modulos"] = [
    {
        "mac": "AAA111",
        "ni": "ROUTER",
        "id": "0x0009"
    },
    {
        "mac": "BBB222",
        "ni": "COORDINADOR",
        "id": "0x0009"
    }
    ]
    Y acabas de configurar un módulo cuya MAC es: 
    
    mac = configuracion["MAC"] ="BBB222"

    Y además, modulo contiene la información nueva de ese XBee

    modulo = {
    "mac": "BBB222",
    "ni": "NUEVO_NOMBRE",
    "id": "0x0009",
    ...
    }

    Ahora empieza: 

    for indice, anterior in enumerate(registro["modulos"]):     # Donde enumerate() toma cada elemento de la lista y además te da su posición.

    Primera vuelta:

    indice = 0

    anterior = {
        "mac": "AAA111",
        "ni": "ROUTER",
        "id": "0x0009"
    }

    Y entonces anterior.get("mac") = "AAA111" que no es igual a mac = "BBB222", entonces no entra al if y sigue con la siguiente vuelta del for.

    Segunda vuelta:

    indice = 1

    anterior = {
        "mac": "BBB222",
        "ni": "COORDINADOR",
        "id": "0x0009"
    }

    Ahora anterior.get("mac") = "BBB222" que es igual a mac = "BBB222", entonces entra al if y ejecuta:

    registro["modulos"][indice] = modulo

    como inidice = 1, entonces registro["modulos"][1] = modulo, es decir, se reemplaza el segundo elemento de la lista por el nuevo diccionario modulo. Y luego hace break y sale del for.

    Como hubo break, entonces el else no se ejecuta y no se añade un nuevo elemento a la lista. La lista sigue teniendo dos elementos, pero el segundo ahora tiene la información actualizada del XBee.
    """                     

    escribirRegistro(registro)  # Guarda ese diccionario en: xbee_configurados.json


def mostrarRegistro():
    """Muestra lo guardado en el JSON"""
    registro = cargarRegistro()
    modulos = registro["modulos"]

    print()
    print(f"Registro: {RUTA_REGISTRO}")

    if not modulos: # Si la lista de modulos está vacía, entonces imprime el mensaje y retorna de la función.
        print("Todavía no se han registrado módulos.")
        return

    print()

    for indice, modulo in enumerate(modulos, start=1):
        print(
            f"{indice}. {modulo.get('ni', '(sin NI)')} | "
            f"MAC {modulo.get('mac', '?')} | "
            f"ID {modulo.get('id', '?')} | "
            f"AP {modulo.get('ap', '?')} | "
            f"CE {modulo.get('ce', '?')}"
        )
        if modulo.get("ids_por_verificar"):
            print("   Configuración pendiente de verificar; ID posibles: "
                  + ", ".join(modulo["ids_por_verificar"]))

    """
    Ejemplo de salida:
    Registro: xbee_configurados.json
    
    Ahora, supongamos que el registro contiene dos módulos:

    indice = 1
    modulo = {
    "mac": "0013A20041AAAAAA",
    "ni": "ROUTER",
    "id": "0x0009",
    "ap": 1,
    "ce": 0
    }


    Por lo tanto, gracias a este último bloque se convierte realmente en:

    print(
        f"1. ROUTER | "
        f"MAC 0013A20041AAAAAA | "
        f"ID 0x0009 | "
        f"AP 1 | "
        f"CE 0"
    )

    y por tanto imprime:

    1. ROUTER | MAC 0013A20041AAAAAA | ID 0x0009 | AP 1 | CE 0


    LUEGO , el segundo modulo del registro es:

    indice = 2
    modulo = {
    "mac": "0013A20041BBBBBB",
    "ni": "COORDINADOR",
    "id": "0x0009",
    "ap": 1,
    "ce": 1
    }

    Entonces imprime:

    2. COORDINADOR | MAC 0013A20041BBBBBB | ID 0x0009 | AP 1 | CE 1
    """


def guardarDescubrimiento(configuracionLocal, nodos):
    """Guarda la última búsqueda sin borrar el historial de módulos configurados."""
    registro = cargarRegistro()
    fecha = fechaHoraActual()
    for nodo in nodos:
        if nodo["mac"] == configuracionLocal["MAC"]:
            continue  # NO puede incluir el propio radio; no es un módulo remoto.

        modulo = next(
            (anterior for anterior in registro["modulos"]
             if anterior.get("mac") == nodo["mac"]),
            None,
        )
        if modulo is None:
            modulo = {"mac": nodo["mac"]}
            registro["modulos"].append(modulo)

        # ND informa MAC, NI y RSSI, pero no informa AP ni CE. Si ya se habían
        # leído, se conservan; descubrir un nodo no equivale a configurarlo.
        modulo.update({
            "ni": nodo["ni"],
            "id": f"0x{configuracionLocal['ID']:04X}",
            "ultimo_avistamiento": fecha,
            "descubierto_desde": configuracionLocal["MAC"],
            "rssi_dbm": nodo.get("rssi_dbm"),
        })

    registro["ultimo_descubrimiento"] = {
        "fecha": fecha,
        "id_red": f"0x{configuracionLocal['ID']:04X}",
        "coordinador_local": configuracionLocal["MAC"],  # Clave conservada del JSON anterior.
        "nodos": nodos,
    }
    escribirRegistro(registro)


# *********************** TRAMAS API XBEE ************************ #

# Estas funciones se utilizan para ND y la nueva comunicación remota.
# La lectura y configuración local del menú siguen usando comandos AT de texto.
# 'modo' es MODO_API_1 o MODO_API_2: son constantes de texto del programa,
# no los números 1 y 2 que se guardan en el parámetro AP del XBee.

# Operación API (AP = 1): estructura de la trama sin escapes.
# | Campo               | Byte  | Descripción                                      |
# |---------------------|-------|--------------------------------------------------|
# | Delimitador inicial | 1     | 0x7E                                             |
# | Longitud            | 2 - 3 | Byte más significativo, byte menos significativo |
# | Datos de la trama   | 4 - n | Estructura específica del tipo de trama          |
# | Checksum            | n + 1 | 1 byte                                           |

# Operación API con caracteres escapados (AP = 2): solo UART, no SPI.
# | Campo               | Byte  | Descripción                                      | Escape si hace falta |
# |---------------------|-------|--------------------------------------------------|----------------------|
# | Delimitador inicial | 1     | 0x7E                                             | No                   |
# | Longitud            | 2 - 3 | Byte más significativo, byte menos significativo | Sí                   |
# | Datos de la trama   | 4 - n | Estructura específica del tipo de trama          | Sí                   |
# | Checksum            | n + 1 | 1 byte                                           | Sí                   |
# Las posiciones de API 2 indican bytes lógicos; los escapes pueden ocupar
# bytes adicionales en UART. El delimitador inicial nunca se escapa.

def escaparDatosApi(datos):
    """Prepara los bytes reservados para transmitirlos en API 2."""
    resultado = bytearray() # Crea un arreglo de bytes modificable


    """
    Data bytes that need to be escaped: 

    0x7E --- Frame Delimiter
    0x7D --- Escape
    0x11 --- XON
    0x13 --- XOFF

    """
    for byte in datos:  # Al recorrer bytes se obtiene un entero de 0 a 255. Por ejemplo: datos = b"\x7E\x01" ---> Primera vuelta: byte = 126 = 0x7E, Segunda vuelta: byte = 1   = 0x01
        if byte in (0x7E, 0x7D, 0x11, 0x13):    # Cuando encuentra un byte reservado
            resultado.append(0x7D)              # A palabras del manual del fabricante: "specific data values must be escaped (flagged) so they do not interfere with the data frame sequencing. To escape an interfering data byte, insert0x7D and follow it with the byte to be escaped XOR’d with 0x20."
            resultado.append(byte ^ 0x20)       # ^ es XOR; no es una potencia.
        else:
            resultado.append(byte)

    # Ejemplo: 0x7E se transmite como 0x7D 0x7E^0x20  (lo cual da 0x5E).
    # El receptor recupera 0x7E aplicando 0x5E ^ 0x20. Solo se cambia temporalmente su representación en el puerto serial.
    return bytes(resultado) # Al final se convierte a bytes, que es inmutable


def crearTramaApi(datosTrama, modo):
    """Añade inicio, longitud y checksum a los datos internos de una trama."""
    if modo not in (MODO_API_1, MODO_API_2):
        raise ErrorXBee("para crear una trama debe seleccionarse API 1 o API 2")

    
    
    
    longitud = len(datosTrama).to_bytes(2, byteorder="big") # La longitud cuenta solo datosTrama, antes de agregar escapes. # to_bytes(2, 'big') ocupa dos bytes: el más significativo va primero.
    """
    Ejemplo: 
    datosTrama = 08 01 4E 44

    datosTrama = bytes([
    0x08,  # Tipo de trama AT
    0x01,  # Identificador
    0x4E,  # N
    0x44,  # D
    ])

    Hay cuatro bytes de datos, len(datosTrama) == 4 
    Que tienen que ser representados en 2 bytes, por lo tanto 4 = 00 04
    De esta manera: ---> longitud = b'\x00\x04'.
    """


    # XBee establece que: La suma de los bytes de datosTrama más el checksum debe terminar en 0xFF
    # Por lo tanto, sum(datosTrama) + checksum = 0xFF
    # checksum = 0xFF - sum(datosTrama)

    checksum = bytes([(0xFF - sum(datosTrama)) & 0xFF]) # 11111111 = 255 = 0xFF. El operador & es AND bit a bit. Aplicarlo con 0xFF conserva solamente los ocho bits inferiores.

    """
    datosTrama = 08 01 4E 44
    La suma es 0x08 + 0x01 + 0x4E + 0x44 = 0x9B (155 en decimal)

    Entonces: 
    checksum = 0xFF - 0x9B  (0xFF es 255 en decimal, entonces 255-155 = 100)
    checksum = 0x64

    Donde la condición final es sum(datosTrama) + checksum = 0xFF ---> 0x9B + 0x64 = 0xFF

    el & sirve para: 

      01100100    0x64
    & 11111111    0xFF
      --------
      01100100    0x64

    Por lo tanto, resultado sigue siendo:

    0x64

    No se está comprobando que 0x64 sea igual a 0xFF. Como los ocho bits de 0xFF son unos, conserva los ocho bits de 0x64.

    FInalmente, bytes([0x64]) produce un objeto bytes con el checksum: b"\x64"
    """

    contenido = longitud + datosTrama + checksum    # La trama completa sería:  Start Delimiter (0x7E) + contenido (longitud + datosTrama + checksum)

    if modo == MODO_API_2:
        contenido = escaparDatosApi(contenido)

    return b"\x7E" + contenido  # Trama completa. El delimitador inicial nunca se escapa.


def leerByteApi(puerto, modo, tiempoFinal):
    """Leer y devolver un byte original de la trama, eliminando el escape de API 2 cuando sea necesario."""
    escapado = False

    while time.monotonic() < tiempoFinal:
        dato = puerto.read(1)               #Solicita un byte al puerto. read(1) no devuelve directamente un número. Devuelve un objeto bytes, es decir, un contenedor de bytes (en este caso, de un byte) de la forma b"O" por ejemplo

        if not dato:
            continue

        byte = dato[0]  # Para obtener el valor numérico de ese único byte del objeto bytes  b"\x5E", se usa byte = dato[0] --->  dato = b"\x5E" ---> byte = dato[0] = 95 (decimal) = x5E (Hexa)

        if escapado:            # Esta condición solamente será verdadera si en la vuelta anterior se recibió 0x7D
            return byte ^ 0x20  # Aunque ambos operandos se escriban como enteros, Python aplica XOR entre sus bits

        if modo == MODO_API_2 and byte == 0x7D:     # Solo en el modo AP =2 hay datos escapados, y el indicador de que el próximo byte es escapado es 0x7D
            escapado = True
            continue

        return byte # Se llega aquí cuando: el byte no está precedido por un escape o se está utilizando API 1, donde no se procesan escapes.

    if escapado:                                                # Se recibió 7D pero nunca llegó el byte siguiente
        raise ErrorXBee("se recibió un escape API incompleto")

    raise TimeoutError


def leerTramaApi(puerto, modo, tiempoEspera=TIEMPO_RESPUESTA_API_S):
    """Reconstruye una trama completa, valida checksum y conserva bytes pendientes.

    El buffer pertenece al puerto: una lectura que agota su tiempo no destruye
    una trama recibida a medias. El ruido y las tramas corruptas se descartan;
    los datos de sensores se devuelven completos para que el llamador los filtre.
    """
    if modo not in (MODO_API_1, MODO_API_2):
        raise ErrorXBee("para leer una trama debe seleccionarse API 1 o API 2")
    tiempoFinal = time.monotonic() + tiempoEspera
    estado = getattr(puerto, "_bufferTramaApi", None)
    if estado is None or estado["modo"] != modo:
        estado = {"modo": modo, "datos": bytearray(), "ultimo_byte": time.monotonic()}
        puerto._bufferTramaApi = estado
    buffer = estado["datos"]

    while time.monotonic() < tiempoFinal:
        # Busca el delimitador inicial. Este byte nunca se escapa.
        inicio = buffer.find(b"\x7E")
        if inicio < 0:
            buffer.clear()
        elif inicio:
            del buffer[:inicio]
        contenido = bytearray()
        consumidos = 1
        escapado = False
        reiniciar = False
        longitud = None
        for byte in buffer[1:]:
            consumidos += 1
            if modo == MODO_API_2:
                if byte == 0x7E:
                    # En API 2 un delimitador sin escape empieza otra trama.
                    del buffer[:consumidos - 1]
                    reiniciar = True
                    break
                if escapado:
                    byte ^= 0x20
                    escapado = False
                elif byte == 0x7D:
                    escapado = True
                    continue
            contenido.append(byte)
            if len(contenido) == 2:
                byteLongitudAlto = contenido[0]
                byteLongitudBajo = contenido[1]
                longitud = (byteLongitudAlto << 8) | byteLongitudBajo  # << 8 desplaza el byte alto ocho posiciones; | reúne ambos bytes.
                if not 1 <= longitud <= MAX_LONGITUD_API:
                    del buffer[0]
                    reiniciar = True
                    break
            if longitud is not None and len(contenido) == longitud + 3:
                datosTrama = contenido[2:-1]
                checksum = contenido[-1]
                if ((sum(datosTrama) + checksum) & 0xFF) != 0xFF:
                    # Puede haber otra trama válida después del byte dañado.
                    del buffer[0]
                    reiniciar = True
                    break
                del buffer[:consumidos]
                return bytes(datosTrama)
        if reiniciar:
            continue

        dato = puerto.read(min(max(getattr(puerto, "in_waiting", 0), 1), 512))
        if dato:
            buffer.extend(dato)
            estado["ultimo_byte"] = time.monotonic()
        elif buffer and time.monotonic() - estado["ultimo_byte"] > TIEMPO_ENTRE_BYTES_API_S:
            # Una cabecera incompleta o falsa no debe bloquear todas las
            # respuestas que lleguen después. Se busca el siguiente 7E.
            del buffer[0]
    raise TimeoutError

    """
    Supongamos que los bytes de longitud son: 0x01 0x02

    Los valores por separado son:

    byteLongitudAlto = 0x01
    byteLongitudBajo = 0x02

    Primero se desplaza el alto:

    0x01 << 8

    00000001              byte alto original
            ↓
    00000001 00000000     desplazado ocho posiciones

    El byte bajo es:

    00000000 00000010

    Se aplica OR:

       00000001 00000000
    |  00000000 00000010
       -----------------
       00000001 00000010

    El resultado es:

    0x0102

    En decimal:

    258

    Lo que significa que el campo datosTrama contiene exactamente 258 bytes

    """


def siguienteIdTrama():
    """Devuelve identificadores consecutivos entre 1 y 255 para las tramas API."""

    # Conserva el valor anterior, le suma uno y vuelve a 1 después de 255.
    # Nunca devuelve 0 porque ese valor indica que no se solicita respuesta.
    siguienteIdTrama.valor = (siguienteIdTrama.valor % 255) + 1

    return siguienteIdTrama.valor


# Crea el atributo que almacena el último identificador utilizado.
# Al comenzar vale 0, por lo que la primera llamada devolverá 1.
siguienteIdTrama.valor = 0


def enviarComandoApi(puerto, modo, comando, parametro=None, mac=None,
                     tiempoEspera=None):
    """Envía un AT local (0x08) o remoto (0x17) y espera su propia respuesta.

    mac=None selecciona el XBee local. Una MAC de 16 cifras selecciona un
    único remoto. Las escrituras remotas quedan pendientes hasta enviar AC;
    así se puede guardar con WR antes de aplicar un cambio de red.
    Devuelve NI como texto, las consultas numéricas como int y escrituras/
    controles como 'OK', igual que enviarComandoTexto.
    """
    # Solo las consultas se repiten: duplicar WR o AC tras perder su respuesta
    # podría guardar o aplicar algo distinto de lo que pretendía el usuario.
    intentos = 2 if mac and parametro is None and comando not in ("AC", "WR", "CN") else 1
    for intento in range(intentos):
        try:
            identificador = siguienteIdTrama()
            comandoBytes = comando.encode("ascii")
            if len(comandoBytes) != 2:
                raise ErrorXBee("un comando API debe tener dos caracteres")

            if mac is None:
                datosTrama = bytes([0x08, identificador]) + comandoBytes
            else:
                try:
                    direccion = bytes.fromhex(mac)
                except (TypeError, ValueError) as error:
                    raise ErrorXBee(f"MAC no válida: {mac!r}") from error
                if len(direccion) != 8 or int.from_bytes(direccion, "big") in (0, 0xFFFF):
                    raise ErrorXBee("la configuración remota exige una MAC unicast de 64 bits")

                # 0xFFFE es el campo reservado. Opciones=0 conserva ACK y no activa
                # 'Apply changes': ID, AP, CE y NI se preparan antes de WR y AC.
                datosTrama = (bytes([0x17, identificador]) + direccion
                              + b"\xFF\xFE\x00" + comandoBytes)

            if parametro is not None:
                if comando == "NI":
                    datosTrama += parametro.encode("ascii")
                else:
                    # En API se envía el número binario, no su texto hexadecimal.
                    # ID=0x1234 se transmite como dos bytes 0x12 y 0x34.
                    cantidad = 2 if comando in ("ID", "NT") else max(1, (parametro.bit_length() + 7) // 8)
                    datosTrama += parametro.to_bytes(cantidad, "big")

            if tiempoEspera is None:
                tiempoEspera = TIEMPO_RESPUESTA_REMOTA_S if mac else TIEMPO_RESPUESTA_API_S

            # No se vacía el puerto: podría cortarse una trama recibida por radio.
            puerto.write(crearTramaApi(datosTrama, modo))
            puerto.flush()
            tiempoFinal = time.monotonic() + tiempoEspera

            while time.monotonic() < tiempoFinal:
                try:
                    respuesta = leerTramaApi(puerto, modo, tiempoFinal - time.monotonic())
                except TimeoutError:
                    break

                if mac is None:
                    # Respuesta local: tipo, identificador, comando (2), estado, datos.
                    if len(respuesta) < 5 or respuesta[0] != 0x88:
                        continue
                    posicionComando = 2
                else:
                    # Respuesta remota: tipo, identificador, MAC (8), reservado (2),
                    # comando (2), estado, datos. También se comprueba la MAC emisora.
                    if len(respuesta) < 15 or respuesta[0] != 0x97:
                        continue
                    if respuesta[2:10] != direccion:
                        continue
                    posicionComando = 12

                if respuesta[1] != identificador or respuesta[posicionComando:posicionComando + 2] != comandoBytes:
                    continue

                estado = respuesta[posicionComando + 2]
                if estado != 0:
                    descripcion = NOMBRES_ESTADO_AT.get(estado, f"estado {estado}")
                    raise ErrorComandoApi(f"AT{comando} ({mac or 'local'}): {descripcion}", estado)

                datos = respuesta[posicionComando + 3:]
                if parametro is not None or comando in ("AC", "WR", "CN"):
                    return "OK"
                if comando == "NI":
                    return datos.decode("ascii", errors="replace").rstrip("\x00")
                if not datos:
                    raise ErrorXBee(f"AT{comando} devolvió una consulta numérica vacía")
                return int.from_bytes(datos, "big")

            raise ErrorComandoApi(f"no llegó respuesta a AT{comando} ({mac or 'local'})")
        except ErrorComandoApi as error:
            if error.estado not in (None, 4) or intento == intentos - 1:
                raise
            time.sleep(0.250)



def cambiarIdTemporal(puerto, modo, idRed):
    """Aplica y comprueba otro ID local sin guardarlo en memoria no volátil."""
    if not 0 <= idRed <= ID_MAXIMO_900HP:
        raise ErrorXBee("el ID del 900HP debe estar entre 0000 y 7FFF")
    # La sesión ya está en API. 0x08 aplica ID sin repetir +++ por cada red.
    # Nunca se envía WR al coordinador durante una búsqueda o visita remota.
    try:
        enviarComandoApi(puerto, modo, "ID", idRed)
    except ErrorComandoApi as error:
        if error.estado not in (None, 4):
            raise
        # La respuesta se pudo perder después de que el XBee cambiara el ID.
        # La lectura posterior determina si realmente se aplicó el cambio.

    ultimoError = None
    for intento in range(3):
        try:
            if enviarComandoApi(puerto, modo, "ID") == idRed:
                time.sleep(0.100)
                return
        except ErrorComandoApi as error:
            if error.estado not in (None, 4):
                raise
            ultimoError = error
        if intento < 2:
            time.sleep(0.250)

    raise ErrorXBee(f"no se pudo verificar el ID local 0x{idRed:04X}") from ultimoError


@contextmanager
def protegerRestauracion():
    """Evita que un segundo Ctrl+C interrumpa la recuperación ya en curso."""
    manejadorAnterior = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, manejadorAnterior)


@contextmanager
def conservarRedLocal(puerto, modo, configuracionLocal):
    """Verifica ID/AP al salir; usa texto AT si la interfaz API deja de responder."""
    idOriginal = configuracionLocal["ID"]
    apOriginal = configuracionLocal["AP"]
    errorOperacion = None
    try:
        yield
    except BaseException as error:
        errorOperacion = error
        raise
    finally:
        print(f"\nRestaurando ID local 0x{idOriginal:04X} y AP={apOriginal}...")
        # Se mantiene el puerto abierto hasta completar la restauración.
        # No se ocultan fallos: si se desconecta el CP2102, se indica que el
        # ID activo no pudo verificarse. El ID guardado con WR sigue intacto.
        ultimoError = None
        with protegerRestauracion():
            for intento in range(3):
                try:
                    if intento < 2:
                        # Si se consultó la misma red, no hace falta escribir ID.
                        if enviarComandoApi(puerto, modo, "ID") != idOriginal:
                            cambiarIdTemporal(puerto, modo, idOriginal)
                        if enviarComandoApi(puerto, modo, "AP") != apOriginal:
                            raise ErrorXBee("el AP activo cambió durante la operación")
                    else:
                        # Último recurso: +++ sigue disponible con AP=0, 1 o 2.
                        # Ningún cambio de recuperación se guarda con WR.
                        entrarModoComando(puerto, TIEMPO_GUARDA_INICIAL_S)
                        try:
                            enviarComandoTexto(puerto, "ID", idOriginal)
                            if enviarComandoTexto(puerto, "ID") != idOriginal:
                                raise ErrorXBee("el ID recuperado por AT no coincide")
                        finally:
                            salirModoComando(puerto, apOriginal)
                    print(f"ID local restaurado y verificado: 0x{idOriginal:04X}; AP={apOriginal}.")
                    break
                except (ErrorXBee, serial.SerialException, OSError) as error:
                    ultimoError = error
                    hasta = getattr(puerto, "_descubrimientoHasta", None)
                    if intento == 0 and hasta is not None and time.monotonic() < hasta:
                        print("Esperando que finalice el ND cancelado antes de recuperar la red...")
                        while time.monotonic() < hasta:
                            try:
                                leerTramaApi(puerto, modo, min(0.5, hasta - time.monotonic()))
                            except (TimeoutError, ErrorXBee):
                                pass
                        puerto._descubrimientoHasta = None
            else:
                detalle = f"Error inicial: {errorOperacion}. " if errorOperacion is not None else ""
                raise ErrorXBee(
                    f"{detalle}NO se pudo confirmar la restauración del ID local a "
                    f"0x{idOriginal:04X} y AP={apOriginal}. No se guardaron valores "
                    f"temporales con WR. Causa: {ultimoError}"
                ) from ultimoError


def obtenerTiempoDescubrimiento(puerto, modo, tiempoNt):
    """Consulta N? (milisegundos) para respetar la espera real de la red."""
    try:
        tiempoReal = enviarComandoApi(puerto, modo, "N?") / 1000.0
    except ErrorComandoApi as error:
        if error.estado not in (1, 2, 3):
            raise
        # Algunos firmwares no ofrecen N?. Se conserva NT y el margen usual.
        tiempoReal = tiempoNt
    return max(tiempoNt, tiempoReal) + 2.0


def pedirRangoRed():
    """Acepta un intervalo hexadecimal inclusivo, un ID, o Enter para todos."""
    while True:
        texto = input("Establecer rango de búsqueda de red [0000 - 7FFF]: ").strip()
        if not texto:
            return 0, ID_MAXIMO_900HP
        try:
            partes = texto.replace("–", "-").split("-")
            if len(partes) not in (1, 2):
                raise ValueError
            inicio = int(partes[0].strip(), 16)
            fin = int(partes[-1].strip(), 16)
            if 0 <= inicio <= fin <= ID_MAXIMO_900HP:
                return inicio, fin
        except ValueError:
            pass
        print("Use, por ejemplo, 0009-0010 o un solo ID. El 900HP admite 0000-7FFF.")


def elegirRed(idActual, accion):
    """Submenú compartido para descubrir o configurar; 0 vuelve al menú."""
    print()
    print(f"  1. {accion} dentro de la misma red [ID: {idActual:04X}]")
    print(f"  2. {accion} dentro de otra red")
    print("  0. Volver al menú principal\n")
    
    while True:
        opcion = input("Seleccione una opción: ").strip()
        if opcion in ("0", "1", "2"):
            return opcion
        print("Seleccione 0, 1 o 2.")


def registrarEstadoBusqueda(estado):
    """Conserva el progreso; al cancelar se repite el ID que quedó incompleto."""
    registro = cargarRegistro()
    registro["busqueda_redes"] = dict(estado)
    escribirRegistro(registro)


def buscarEnRango(puerto, modo, configuracion, opcionesNo, tiempoNt, tiempoEspera,
                  rutaPuerto=None):
    """Prueba los ID en orden y conserva cada nodo incluso si se interrumpe ND."""
    inicio, fin = pedirRangoRed()
    total = fin - inicio + 1
    duracionHoras = total * (INTENTOS_DESCUBRIMIENTO * (tiempoEspera + TIEMPO_RESPUESTA_API_S) + 0.1) / 3600
    print(f"\nSe probarán {total} ID. Presupuesto de espera: unas {duracionHoras:.2f} horas.")
    print("Puede terminar antes si ND indica el final. Ctrl+C cancela y restaura el ID local.")
    print("El barrido solo cambia ID; HP, CM y cifrado deben ser compatibles.")
    print("Se hará un segundo intento en los ID sin respuestas; esto aumenta la duración máxima.")

    estado = {
        "inicio": f"0x{inicio:04X}", "fin": f"0x{fin:04X}",
        "coordinador_local": configuracion["MAC"],
        "id_original": f"0x{configuracion['ID']:04X}",
        "id_actual": None, "siguiente_id": f"0x{inicio:04X}",
        "estado": "en_curso", "fecha_inicio": fechaHoraActual(),
    }
    encontrados = {}
    registrarEstadoBusqueda(estado)
    try:
        for numero, idRed in enumerate(range(inicio, fin + 1), start=1):
            estado["id_actual"] = f"0x{idRed:04X}"
            estado["siguiente_id"] = f"0x{idRed:04X}"
            registrarEstadoBusqueda(estado)
            print(f"\n[{numero}/{total}] Buscando en ID 0x{idRed:04X}...")
            cambiarIdTemporal(puerto, modo, idRed)
            configuracionRed = dict(configuracion, ID=idRed)

            def alEncontrar(nodo):
                if nodo["mac"] != configuracion["MAC"]:
                    nodo["id"] = f"0x{idRed:04X}"
                    guardarDescubrimiento(configuracionRed, [nodo])
                    encontrados[nodo["mac"]] = nodo

            nodos = descubrirNodosApi(
                puerto, modo, opcionesNo, tiempoNt,
                alEncontrar=alEncontrar, tiempoEspera=tiempoEspera, macLocal=configuracion["MAC"],
            )
            if rutaPuerto is not None:
                verificarConfiguracionesPendientes(
                    puerto, modo, configuracionRed, nodos, rutaPuerto,
                )
            if not any(nodo["mac"] != configuracion["MAC"] for nodo in nodos):
                print(f"Sin respuestas remotas en ID 0x{idRed:04X}.")
            estado["siguiente_id"] = f"0x{idRed + 1:04X}" if idRed < fin else None
            registrarEstadoBusqueda(estado)
        estado["estado"] = "completada"
    except KeyboardInterrupt:
        estado["estado"] = "cancelada"
        print("\nBúsqueda cancelada. Se conservan los módulos ya encontrados.")
    except Exception:
        estado["estado"] = "interrumpida_por_error"
        raise
    finally:
        estado["fecha_fin"] = fechaHoraActual()
        registrarEstadoBusqueda(estado)
        if estado["siguiente_id"] is not None:
            print(f"Para continuar después, use el rango {estado['siguiente_id']}-{estado['fin']}.")
    return list(encontrados.values())



# ********************* DESCUBRIMIENTO DE RED ******************** #

def prepararDescubrimientoApi(puerto):
    """Lee por AT los datos necesarios y confirma ATCN antes de permitir API.

    Devuelve (configuracion, modoApi, opcionesNo, tiempoNt).
    NO indica los campos adicionales de ND y NT su duración en décimas
    de segundo. Consultarlos no cambia sus valores ni el AP del XBee.
    """
    apOriginal = entrarModoComandoAislado(puerto)

    try:
        configuracion = leerRegistrosConfiguracion(puerto)

        # La lectura interna encuentra el AP=0 temporal. El descubrimiento y
        # la información mostrada deben utilizar el AP original del XBee.
        configuracion["AP"] = apOriginal
        ap = apOriginal

        if ap not in (1, 2):
            raise ErrorXBee(
                f"el XBee local tiene AP={ap}. Esta opción utiliza ND mediante "
                "tramas 0x08/0x88 y necesita AP=1 o AP=2. Puede elegirlo en la "
                "opción 2; no se cambiará el AP definitivo automáticamente"
            )

        opcionesNo = enviarComandoTexto(puerto, "NO")
        tiempoNt = enviarComandoTexto(puerto, "NT") * 0.100
        modoApi = MODO_API_1 if ap == 1 else MODO_API_2

    finally:
        # Se confirma AP y ATCN incluso si falló una consulta de preparación.
        salirModoComando(puerto, apOriginal)

    time.sleep(0.100)  # Pequeña separación entre la sesión de texto y ND binario.
    return configuracion, modoApi, opcionesNo, tiempoNt


def analizarRespuestaNd(datos, opcionesNo):
    """Interpreta ND DigiMesh y el formato antiguo de la documentación 900HP.

    DigiMesh: MY (2), SH (4), SL (4), NI y 0x00, padre (2), tipo (1),
    estado (1), perfil (2), fabricante (2), y los campos opcionales de NO.
    MY y padre valen FFFE. No hay un byte RSSI antes del nombre en este formato.
    El formato antiguo empieza por SH/SL y sí incluye DB antes de NI.
    """
    if len(datos) < 16:
        raise ErrorXBee("se recibió una respuesta ND demasiado corta")

    formatoDigimesh = datos[:2] == b"\xff\xfe"
    inicioMac = 2 if formatoDigimesh else 0
    inicioNi = 10 if formatoDigimesh else 9
    # En un corte [inicio:fin], fin no se incluye.
    sh = bytesAEntero(datos[inicioMac:inicioMac + 4])
    sl = bytesAEntero(datos[inicioMac + 4:inicioMac + 8])
    mac = f"{sh:08X}{sl:08X}"
    if not mac.startswith("0013A2"):
        raise ErrorXBee(f"ND no contiene una MAC Digi válida: {mac}")
    try:
        finNi = datos.index(0x00, inicioNi)
    except ValueError as error:
        raise ErrorXBee("la respuesta ND no contiene el final de NI") from error
    nombreBytes = datos[inicioNi:finNi]
    if len(nombreBytes) > 20 or any(not 0x20 <= byte <= 0x7E for byte in nombreBytes):
        raise ErrorXBee("ND contiene un NI no válido; no se registrará una MAC dudosa")
    ni = nombreBytes.decode("ascii")
    posicion = finNi + 1
    if formatoDigimesh:
        if datos[posicion:posicion + 2] != b"\xff\xfe":
            raise ErrorXBee("ND DigiMesh no contiene el campo padre FFFE esperado")
        posicion += 2
    if len(datos) < posicion + 6:
        raise ErrorXBee("la respuesta ND no contiene todos los campos básicos")
    tipoNodo = datos[posicion]
    estado = datos[posicion + 1]
    profileId = int.from_bytes(datos[posicion + 2:posicion + 4], "big")
    manufacturerId = int.from_bytes(datos[posicion + 4:posicion + 6], "big")
    posicion += 6
    if tipoNodo not in NOMBRES_TIPO_NODO or estado != 0:
        raise ErrorXBee("ND contiene campos desplazados o un tipo/estado no válido")

    digiDeviceType = None
    rssiUltimoSalto = None
    # & comprueba un bit sin exigir que los demás sean cero.
    # NO=5 (binario 101) activa tanto 0x01 como 0x04.
    if opcionesNo & 0x01:
        if len(datos) < posicion + 4:
            raise ErrorXBee("la respuesta ND no contiene el tipo Digi indicado por NO")
        digiDeviceType = int.from_bytes(datos[posicion:posicion + 4], "big")
        posicion += 4
    if opcionesNo & 0x04:
        if len(datos) <= posicion:
            raise ErrorXBee("la respuesta ND no contiene el RSSI adicional indicado por NO")
        rssiUltimoSalto = -datos[posicion] if datos[posicion] <= 127 else None
        posicion += 1
    if posicion != len(datos):
        raise ErrorXBee("la longitud ND no coincide con sus campos y las opciones NO")

    # Un RSSI ausente no equivale a cero. En DigiMesh solo se usa el campo
    # adicional de NO; nunca se toma un byte de la MAC como si fuera señal.
    if formatoDigimesh:
        rssi = rssiUltimoSalto
    else:
        if datos[8] > 127:
            raise ErrorXBee("ND antiguo contiene un RSSI fuera de rango")
        rssi = -datos[8]  # Ejemplo: el byte 64 representa -64 dBm.

    # Se devuelven valores ya interpretados para mostrarlos o guardarlos en JSON.
    return {
        "mac": mac, "sh": f"{sh:08X}", "sl": f"{sl:08X}", "ni": ni,
        "rssi_dbm": rssi, "tipo_nodo": tipoNodo,
        "tipo_nodo_texto": NOMBRES_TIPO_NODO[tipoNodo], "estado": estado,
        "profile_id": f"0x{profileId:04X}",
        "manufacturer_id": f"0x{manufacturerId:04X}",
        "digi_device_type": f"0x{digiDeviceType:08X}" if digiDeviceType is not None else None,
        "rssi_ultimo_salto_dbm": rssiUltimoSalto,
        "formato_nd": "digimesh" if formatoDigimesh else "900hp_antiguo",
        "nd_hex": datos.hex().upper(),  # Permite revisar la respuesta original si hace falta.
    }


def descubrirNodosApi(puerto, modo, opcionesNo, tiempoNt, alEncontrar=None,
                      tiempoEspera=None, macLocal=None):
    """Envía únicamente ND por API y reúne las respuestas de los distintos nodos.

    Debe llamarse después de prepararDescubrimientoApi. NO y NT ya se leyeron
    por AT. Al terminar se consulta AP por API para confirmar que el enlace
    local sigue respondiendo. Si no hubo nodos remotos, se intenta una vez más.
    El XBee local debe estar fuera de modo comando, operando en API 1 o API 2.
    """
    # Si se conoce N?, el llamador proporciona su espera real con margen.
    # Las llamadas anteriores conservan NT más dos segundos y su límite.
    if tiempoEspera is None:
        tiempoEspera = min(tiempoNt + 2.0, TIEMPO_MAXIMO_DESCUBRIMIENTO_S)

    print()
    print(f"Ejecutando ND. Tiempo máximo de espera: {tiempoEspera:.1f} s...")

    if tiempoNt > tiempoEspera:
        print(
            "Aviso: NT solicita una espera mayor; esta versión la limita a "
            f"{TIEMPO_MAXIMO_DESCUBRIMIENTO_S:.0f} s."
        )

    nodosPorMac = {}  # Una MAC es una clave única: evita duplicar un mismo nodo.
    for intento in range(INTENTOS_DESCUBRIMIENTO):
        if intento:
            print("No respondió ningún remoto; se repite ND una vez antes de continuar.")
        identificador = siguienteIdTrama()
        # 0x08 = comando AT local. El XBee local ejecuta ND para buscar por radio.
        # No es una configuración remota 0x17 y no incluye las letras 'AT' ni CR.
        # Con identificador=1, crearTramaApi produce: 7E 00 04 08 01 4E 44 64.
        datosTrama = bytes([0x08, identificador]) + b"ND"

        # Se conservan las tramas pendientes; se filtran por tipo, comando e ID.
        puerto.write(crearTramaApi(datosTrama, modo))
        puerto.flush()

        tiempoFinal = time.monotonic() + tiempoEspera
        puerto._descubrimientoHasta = tiempoFinal
        errorInterpretacion = None

        while time.monotonic() < tiempoFinal:
            restante = tiempoFinal - time.monotonic()

            try:
                respuesta = leerTramaApi(puerto, modo, restante)
            except TimeoutError:
                break

            # Formato de respuesta: [0x88, ID, 'N', 'D', estado, ...datos ND...].
            # Se ignoran otros tipos, como 0x90 (datos), que no corresponden a ND.
            if len(respuesta) < 5 or respuesta[0] != 0x88:
                continue

            # Un 0x88 por sí solo no basta: debe coincidir el ID y también el comando.
            if respuesta[1] != identificador or respuesta[2:4] != b"ND":
                continue

            estado = respuesta[4]  # 0 significa que el comando se ejecutó sin error.

            if estado != 0:
                descripcion = NOMBRES_ESTADO_AT.get(estado, str(estado))
                raise ErrorXBee(f"la búsqueda ND terminó con estado {descripcion}")

            datos = respuesta[5:]  # Solo el contenido específico de un resultado ND.

            # Una respuesta sin datos marca el final del descubrimiento.
            if not datos:
                puerto._descubrimientoHasta = None
                break

            try:
                nodo = analizarRespuestaNd(datos, opcionesNo)
            except ErrorXBee as error:
                errorInterpretacion = error
                print(f"Aviso: se ignoró una respuesta ND: {error}. Datos: {datos.hex().upper()}")
                continue

            # A diferencia de una consulta AT sencilla, no se retorna aquí:
            # se sigue leyendo porque pueden responder más módulos.
            nodosPorMac[nodo["mac"]] = nodo
            if alEncontrar is not None:
                alEncontrar(nodo)  # Guarda cada hallazgo antes de seguir esperando.
            señal = f"{nodo['rssi_dbm']} dBm" if nodo['rssi_dbm'] is not None else "RSSI no incluido"
            print(
                f"  Encontrado: {nodo['ni'] or '(sin NI)'} | "
                f"{nodo['mac']} | {señal}"
            )

        # Un silencio del puerto no prueba que la red esté vacía. Si el enlace
        # local falló, se informa como error y se ejecuta la recuperación exterior.
        apEsperado = 1 if modo == MODO_API_1 else 2
        if enviarComandoApi(puerto, modo, "AP") != apEsperado:
            raise ErrorXBee("el XBee local dejó de estar en el AP esperado durante ND")
        puerto._descubrimientoHasta = None
        remotos = [n for n in nodosPorMac.values() if n["mac"] != macLocal]
        if remotos:
            break
        if errorInterpretacion is not None:
            raise ErrorXBee(f"ND recibió datos que no pudo interpretar: {errorInterpretacion}")
    return list(nodosPorMac.values())


def mostrarNodosDescubiertos(nodos):
    """Presenta la lista obtenida por ND; no realiza una nueva búsqueda."""
    print()

    if not nodos:
        print("No se encontraron módulos remotos en la búsqueda.")
        return

    print(f"Módulos encontrados: {len(nodos)}")
    print("-" * 74)

    for indice, nodo in enumerate(nodos, start=1):
        print(f"{indice}. NI: {nodo['ni'] or '(sin NI)'}")
        print(f"   MAC: {nodo['mac']}")
        if "id" in nodo:
            print(f"   ID: {nodo['id']}")
        print(f"   Tipo: {nodo['tipo_nodo_texto']}")
        rssi = nodo.get("rssi_dbm")
        print("   RSSI informado por ND: " + (f"{rssi} dBm" if rssi is not None else "no incluido"))

        if nodo["rssi_ultimo_salto_dbm"] is not None:
            print(
                "   RSSI del último salto: "
                f"{nodo['rssi_ultimo_salto_dbm']} dBm"
            )

    print("-" * 74)


# ********************** OPERACIONES DEL MENÚ ******************** #


def comprobarModulosRegistrados(puerto, modo, configuracionLocal):
    """Visita solamente las redes registradas y consulta NI por MAC unicast."""
    registro = cargarRegistro()
    candidatos = list(registro["modulos"])
    macRegistradas = {str(modulo.get("mac", "")).upper() for modulo in candidatos}
    ultimo = registro.get("ultimo_descubrimiento")
    if isinstance(ultimo, dict) and ultimo.get("id_red"):
        # Compatibilidad con el JSON anterior, que guardaba los hallazgos
        # solamente en ultimo_descubrimiento y no en la lista de módulos.
        candidatos.extend(dict(nodo, id=ultimo["id_red"])
                          for nodo in ultimo.get("nodos", [])
                          if str(nodo.get("mac", "")).upper() not in macRegistradas)

    porRed = {}
    for modulo in candidatos:
        mac = str(modulo.get("mac", "")).upper()
        try:
            if len(mac) != 16 or len(bytes.fromhex(mac)) != 8:
                continue
            if mac == configuracionLocal["MAC"] or int(mac, 16) in (0, 0xFFFF):
                continue
        except ValueError:
            continue

        if mac.startswith("FFFE0013"):
            print(f"Registro antiguo inválido: {mac}. Vuelva a descubrir ese módulo con la opción 3.")
            continue

        # Si una configuración quedó sin verificar, se conservan ambas redes
        # posibles para poder reencontrar el nodo, sin afirmar dónde quedó.
        ids = [modulo.get("id")] + modulo.get("ids_por_verificar", [])
        for valor in ids:
            try:
                idRed = int(valor, 16) if isinstance(valor, str) else int(valor)
            except (ValueError, TypeError):
                continue
            if 0 <= idRed <= ID_MAXIMO_900HP and idRed != configuracionLocal["ID"]:
                porRed.setdefault(idRed, {})[mac] = modulo

    disponibles = {}
    for idRed, modulos in sorted(porRed.items()):
        print(f"\nComprobando módulos guardados en ID 0x{idRed:04X}. Ctrl+C para cancelar.")
        cambiarIdTemporal(puerto, modo, idRed)
        for mac in modulos:
            if mac in disponibles:
                continue
            print(f"  Consultando MAC {mac}...")
            try:
                ni = enviarComandoApi(puerto, modo, "NI", mac=mac)
            except ErrorComandoApi as error:
                print(f"  Sin disponibilidad confirmada: {mac}. {error}")
                continue
            nodo = {"mac": mac, "ni": ni, "id": f"0x{idRed:04X}"}
            disponibles[mac] = nodo
            guardarDescubrimiento(dict(configuracionLocal, ID=idRed), [nodo])
            print(f"  Disponible: {ni or '(sin NI)'} | {mac} | ID 0x{idRed:04X}")
    return list(disponibles.values())


def seleccionarModuloRemoto(nodos):
    """Muestra únicamente los nodos encontrados o comprobados en esta operación."""
    print("\nMódulos disponibles para configurar:")
    for indice, nodo in enumerate(nodos, start=1):
        print(f"  {indice}. {nodo['ni'] or '(sin NI)'} | MAC {nodo['mac']} | ID {nodo['id']}")
    print("  0. Volver sin configurar")
    print("\nSi se requiere otro: por favor seleccione la opción 3 del menú: "
                          "Descubrir módulos Xbee.\n")
    
    while True:
        respuesta = input("Seleccione el módulo: ").strip()
        if respuesta == "0":
            return None
        try:
            indice = int(respuesta)
            if 1 <= indice <= len(nodos):
                return nodos[indice - 1]
        except ValueError:
            pass
        print("Escriba el número de un módulo de la lista o 0 para volver.")


def leerConfiguracionRemota(puerto, modo, mac):
    """Lee por radio los registros del remoto; su AP puede ser 0, 1 o 2."""
    configuracion = {}
    for comando in COMANDOS_LECTURA_OBLIGATORIOS:
        configuracion[comando] = enviarComandoApi(puerto, modo, comando, mac=mac)
    configuracion["MAC"] = f"{configuracion['SH']:08X}{configuracion['SL']:08X}"
    if configuracion["MAC"] != mac.upper():
        raise ErrorXBee("la MAC leída no coincide con el módulo seleccionado")
    # Se leen los mismos datos informativos de la opción local. Un registro
    # no soportado queda como None; una pérdida de enlace no se oculta.
    for comando in COMANDOS_LECTURA_ADICIONALES:
        try:
            configuracion[comando] = enviarComandoApi(puerto, modo, comando, mac=mac)
        except ErrorComandoApi as error:
            if error.estado not in (1, 2, 3):
                raise
            configuracion[comando] = None
    configuracion["LECTURA"] = "AT remoto por radio (API 0x17/0x97)"
    return configuracion


def registrarIntentoRemoto(configuracionActual, nuevaConfiguracion):
    """Anota las redes posibles antes de escribir; no anuncia configuración verificada."""
    registro = cargarRegistro()
    mac = configuracionActual["MAC"]
    modulo = next((m for m in registro["modulos"] if m.get("mac") == mac), None)
    if modulo is None:
        modulo = {"mac": mac, "ni": configuracionActual["NI"],
                  "id": f"0x{configuracionActual['ID']:04X}"}
        registro["modulos"].append(modulo)
    modulo["ids_por_verificar"] = list(dict.fromkeys([
        f"0x{configuracionActual['ID']:04X}", f"0x{nuevaConfiguracion['ID']:04X}",
    ]))
    modulo["configuracion_solicitada"] = dict(nuevaConfiguracion)
    modulo["estado_configuracion"] = "pendiente_de_verificar"
    escribirRegistro(registro)


def verificarConfiguracionesPendientes(puerto, modo, configuracionLocal,
                                        nodos, rutaPuerto):
    """Comprueba los cuatro valores de un remoto pendiente visto por ND.

    ND confirma que su MAC responde en esta red, pero no informa AP ni CE.
    Por eso solo se elimina la marca pendiente tras leerlo mediante AT remoto.
    """
    registro = cargarRegistro()
    pendientes = {
        modulo.get("mac"): modulo.get("configuracion_solicitada")
        for modulo in registro["modulos"]
        if modulo.get("estado_configuracion") == "pendiente_de_verificar"
    }
    for nodo in nodos:
        mac = nodo["mac"]
        if mac == configuracionLocal["MAC"] or mac not in pendientes:
            continue
        solicitada = pendientes[mac]
        if not isinstance(solicitada, dict) or any(
            clave not in solicitada for clave in ("ID", "AP", "CE", "NI")
        ):
            print(f"{mac}: no hay cuatro valores solicitados para verificar; sigue pendiente.")
            continue
        try:
            leida = leerConfiguracionRemota(puerto, modo, mac)
        except (ErrorXBee, serial.SerialException, OSError) as error:
            print(f"{mac}: no se pudo comprobar por AT remoto: {error}. Sigue pendiente.")
            continue
        diferencias = comprobarConfiguracion(leida, solicitada)
        if diferencias:
            print(f"{mac}: los valores leídos no coinciden con los solicitados; sigue pendiente.")
            continue
        guardarModuloEnRegistro(leida, rutaPuerto, macLocal=configuracionLocal["MAC"])
        print(f"{mac}: ID, AP, CE y NI confirmados por radio; registro actualizado.")


def configurarModuloRemoto(puerto, modo, actual, nueva, rutaPuerto, macLocal):
    """Prepara parámetros, guarda con WR, aplica con AC y verifica en el ID final."""
    mac = actual["MAC"]
    registrarIntentoRemoto(actual, nueva)
    intentoGuardar = False
    try:
        # Opciones=0 en 0x17 deja los cambios pendientes. En particular ID
        # todavía no separa al remoto de su red mientras se confirma WR.
        for comando in ("CE", "NI", "AP", "ID"):
            enviarComandoApi(puerto, modo, comando, nueva[comando], mac=mac)
        intentoGuardar = True
        enviarComandoApi(puerto, modo, "WR", mac=mac)
    except BaseException:
        # Incluye Ctrl+C. Antes de AC el remoto sigue en la red original.
        # Se intenta deshacer lo preparado. Si WR pudo llegar, se restaura
        # también la memoria no volátil; si no, no se escribe flash.
        print("\nLa preparación no terminó. Recuperando los valores remotos anteriores...")
        with protegerRestauracion():
            try:
                for comando in ("CE", "NI", "AP", "ID"):
                    enviarComandoApi(puerto, modo, comando, actual[comando], mac=mac)
                if intentoGuardar:
                    enviarComandoApi(puerto, modo, "WR", mac=mac)
                enviarComandoApi(puerto, modo, "AC", mac=mac)
                recuperada = leerConfiguracionRemota(puerto, modo, mac)
                if comprobarConfiguracion(recuperada, actual):
                    raise ErrorXBee("los valores remotos anteriores no quedaron verificados")
                # Elimina las redes pendientes sin inventar una configuración
                # nueva ni cambiar la fecha de una configuración anterior.
                registro = cargarRegistro()
                for modulo in registro["modulos"]:
                    if modulo.get("mac") == mac:
                        for clave in ("ids_por_verificar", "configuracion_solicitada", "estado_configuracion"):
                            modulo.pop(clave, None)
                escribirRegistro(registro)
                print("Valores remotos anteriores recuperados y verificados.")
            except (ErrorXBee, serial.SerialException, OSError) as error:
                print(f"No se pudo confirmar la recuperación remota: {error}")
                print("Las redes posibles permanecen en el registro para volver a comprobar el módulo.")
        raise

    # AC puede cambiar el ID antes de que su respuesta alcance al coordinador.
    # La ausencia de respuesta no se toma como éxito: hay que leer el remoto
    # en la red nueva y comparar los cuatro parámetros antes de anunciarlo.
    try:
        try:
            enviarComandoApi(puerto, modo, "AC", mac=mac)
        except ErrorComandoApi as error:
            if error.estado not in (None, 4):
                raise
            print(f"AC sin confirmación por radio. Se comprobará el módulo en ID 0x{nueva['ID']:04X}.")

        cambiarIdTemporal(puerto, modo, nueva["ID"])
        for intento in range(3):
            try:
                final = leerConfiguracionRemota(puerto, modo, mac)
                break
            except ErrorComandoApi as error:
                if error.estado not in (None, 4) or intento == 2:
                    raise ErrorXBee(
                        f"no se pudo verificar el módulo {mac} en ID 0x{nueva['ID']:04X}"
                    ) from error
                time.sleep(0.500)

        diferencias = comprobarConfiguracion(final, nueva)
        if diferencias:
            raise ErrorXBee("la configuración remota no quedó verificada: " + "; ".join(diferencias))
        guardarModuloEnRegistro(final, rutaPuerto, macLocal=macLocal)
    except BaseException:
        print(f"WR ya fue confirmado en {mac}; cancelar ahora no deshace lo guardado.")
        print("La verificación no terminó. Las redes posibles siguen en el registro.")
        raise
    print("Configuración remota guardada y verificada correctamente.")
    return final

def operacionLeer(rutaPuerto, baudios):
    """Opción 1: abre el puerto, consulta todo por AT y actualiza el NI del menú."""
    with abrirPuerto(rutaPuerto, baudios) as puerto:    # La función devuelve un objeto de tipo Serial creado por serial.Serial(...) que representa el puerto abierto. Se utiliza con "with" para asegurarse de que se cierre automáticamente al salir del bloque.
        configuracion = leerConfiguracionLocal(puerto)  #  as puerto significa que guarda ese objeto serial en la variable local puerto.

        """
        serial.Serial(
            port=rutaPuerto,
            baudrate=baudios,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=TIEMPO_ESPERA_SERIAL_S,
            write_timeout=2.0,
        )
        """
    mostrarConfiguracion(configuracion)
    return configuracion["NI"] or None


def operacionConfigurar(rutaPuerto, baudios):
    """Opción 2: lee, pide confirmación, escribe y verifica usando solo texto AT."""
   
    with abrirPuerto(rutaPuerto, baudios) as puerto:            
        configuracionActual = leerConfiguracionLocal(puerto)

    #Al entrar al with, abre la conexión; al salir, Python llama automáticamente a su método de salida y PySerial ejecuta puerto.close()
    mostrarConfiguracion(configuracionActual)
    
    nuevaConfiguracion = pedirNuevaConfiguracion(configuracionActual)

    if not pedirConfirmacion("¿Desea escribir estos valores en el XBee?"):  # Si devuelve False, not False = True, entonces entra al if y ejecuta el bloque de código.
        print("Configuración cancelada. No se modificó el XBee.")
        return configuracionActual["NI"] or None

    print()
    print("Escribiendo configuración...")              # Si sí se desea establecer la configuración...

    with abrirPuerto(rutaPuerto, baudios) as puerto:         # Vuelve a abrir /dev/ttyUSBX
        configurarEnModoComando(puerto, nuevaConfiguracion)

    # Se abre nuevamente el puerto para comprobar el modo activo y los valores.
    time.sleep(0.500)

    with abrirPuerto(rutaPuerto, baudios) as puerto:
        configuracionFinal = leerConfiguracionLocal(puerto)

    diferencias = comprobarConfiguracion(
        configuracionFinal,
        nuevaConfiguracion,
    )
    mostrarConfiguracion(configuracionFinal)

    if diferencias:
        print("La verificación encontró diferencias:")

        for diferencia in diferencias:
            print(f"  - {diferencia}")  # diferencias = ["AP: esperado 1, leído 0"]

        """
        La verificación encontró diferencias:
        - AP: esperado 1, leído 0
        """
        raise ErrorXBee("la configuración no quedó verificada") # Si hay diferencias, se lanza una excepción y no se guarda en el registro.

    # Solo se registra como configurado si la lectura posterior coincide.
    guardarModuloEnRegistro(configuracionFinal, rutaPuerto)

    print("Configuración guardada y verificada correctamente.")
    print(f"Registro actualizado: {RUTA_REGISTRO.name}")
    return configuracionFinal["NI"] or None


def operacionDescubrir(rutaPuerto, baudios):
    """Opción 3: descubre en la misma red o recorre un rango con ID temporal."""
    with abrirPuerto(rutaPuerto, baudios) as puerto:
        configuracion, modo, opcionesNo, tiempoNt = prepararDescubrimientoApi(puerto)
        with conservarRedLocal(puerto, modo, configuracion):
            opcion = elegirRed(configuracion["ID"], "Buscar módulos")
            if opcion == "0":
                return configuracion["NI"] or None
            tiempoEspera = obtenerTiempoDescubrimiento(puerto, modo, tiempoNt)
            if opcion == "1":
                print("ND busca módulos con ID, HP, CM y cifrado compatibles. Ctrl+C para cancelar.")
                nodos = descubrirNodosApi(
                    puerto, modo, opcionesNo, tiempoNt,
                    alEncontrar=lambda nodo: guardarDescubrimiento(configuracion, [nodo]),
                    tiempoEspera=tiempoEspera, macLocal=configuracion["MAC"],
                )
                nodos = [dict(nodo, id=f"0x{configuracion['ID']:04X}")
                         for nodo in nodos if nodo["mac"] != configuracion["MAC"]]
                guardarDescubrimiento(configuracion, nodos)
                verificarConfiguracionesPendientes(
                    puerto, modo, configuracion, nodos, rutaPuerto,
                )
            else:
                nodos = buscarEnRango(
                    puerto, modo, configuracion, opcionesNo, tiempoNt,
                    tiempoEspera, rutaPuerto,
                )
            mostrarNodosDescubiertos(nodos)
            print(f"Resultado guardado en {RUTA_REGISTRO.name}.")
    return configuracion["NI"] or None


def operacionConfigurarRemoto(rutaPuerto, baudios):
    """Opción 5: elige un remoto disponible y restaura siempre la red local."""
    with abrirPuerto(rutaPuerto, baudios) as puerto:
        configuracion, modo, opcionesNo, tiempoNt = prepararDescubrimientoApi(puerto)
        with conservarRedLocal(puerto, modo, configuracion):
            opcion = elegirRed(configuracion["ID"], "Configurar un módulo")
            if opcion == "0":
                return configuracion["NI"] or None
            if opcion == "1":
                tiempoEspera = obtenerTiempoDescubrimiento(puerto, modo, tiempoNt)
                nodos = descubrirNodosApi(
                    puerto, modo, opcionesNo, tiempoNt,
                    alEncontrar=lambda nodo: guardarDescubrimiento(configuracion, [nodo]),
                    tiempoEspera=tiempoEspera, macLocal=configuracion["MAC"],
                )
                nodos = [dict(nodo, id=f"0x{configuracion['ID']:04X}")
                         for nodo in nodos if nodo["mac"] != configuracion["MAC"]]
                if not nodos:
                    print("No se encontraron módulos remotos disponibles en esta red.")
                    return configuracion["NI"] or None
            else:
                nodos = comprobarModulosRegistrados(puerto, modo, configuracion)
                if not nodos:
                    print("No se encuentra ningún módulo guardado disponible en el registro, "
                          "por favor seleccionar la opción 3 del menú: Descubrir módulos Xbee.")
                    return configuracion["NI"] or None

            nodo = seleccionarModuloRemoto(nodos)
            if nodo is None:
                return configuracion["NI"] or None
            cambiarIdTemporal(puerto, modo, int(nodo["id"], 16))
            actual = leerConfiguracionRemota(puerto, modo, nodo["mac"])
            print(f"\nMódulo remoto seleccionado: {actual['MAC']}")
            mostrarConfiguracion(actual)
            nueva = pedirNuevaConfiguracion(actual)
            if nueva["ID"] != actual["ID"]:
                print("El nuevo ID separará este módulo de su red actual. Para verificarlo "
                      "debe ser alcanzable desde el coordinador en la nueva red.")
            if not pedirConfirmacion(f"¿Desea escribir estos valores en el XBee remoto {actual['MAC']}?"):
                print("Configuración remota cancelada.")
                return configuracion["NI"] or None
            final = configurarModuloRemoto(puerto, modo, actual, nueva, rutaPuerto, configuracion["MAC"])
            mostrarConfiguracion(final)
    return configuracion["NI"] or None


def mostrarMenu(rutaPuerto, baudios, niPuertoActual=None):
    """Dibuja el menú y el NI conocido del puerto, sin consultar nuevamente el radio."""
    print()
    print("=" * 58)
    print("CONFIGURADOR DE RED XBEE-PRO 900HP DIGIMESH")
    print("=" * 58)

    lineaPuerto = f"Puerto actual: {rutaPuerto} | {baudios} baudios"

    if niPuertoActual:
        lineaPuerto += f" | NI: {niPuertoActual}"

    print(lineaPuerto)
    print()
    print("  1. Leer configuración del XBee local")
    print("  2. Configurar ID, AP, CE y NI")
    print("  3. Descubrir módulos Xbee")
    print("  4. Mostrar registro de módulos")
    print("  5. Configurar a distancia otro XBee")
    print("  6. Cambiar puerto serial")
    print("  0. Salir\n")
    


# ************************ PROGRAMA PRINCIPAL ********************* #

def main():
    """Procesa los argumentos y mantiene el menú activo hasta elegir Salir."""
    # argparse permite, por ejemplo:
    # python3 configurador_red_xbee_v3.py --puerto /dev/ttyUSB0 --baudios 9600
    analizador = argparse.ArgumentParser(                                        # argparse es un módulo de Python que permite recibir opciones escritas en la terminal.
        description=(                                                            # argparse.ArgumentParser(...) crea un objeto encargado de analizar lo que el usuario escriba
            "Configura XBee-PRO 900HP DigiMesh por CP2102 sin utilizar XCTU."    # description=... es un texto que se muestra al ejecutar python3 configurador_red_xbee.py --help
        )
    )
    analizador.add_argument(
        "--puerto",                                     # “Quiero permitir una opción llamada --puerto" Por tanto, el usuario podrá escribir: python3 configurador.py --puerto /dev/ttyUSB0
        help="Puerto serial, por ejemplo /dev/ttyUSB0",
    )
    analizador.add_argument(
        "--baudios",                                    # python3 configurador.py --baudios 9600
        type=int,                                       # El valor recibido debe convertirse a un entero
        default=BAUDIOS,                                # Si el usuario no escribe --baudios, utiliza automáticamente el valor de la constante BAUDIOS
        help=f"Velocidad serial actual del XBee; predeterminado {BAUDIOS}",
    )
    argumentos = analizador.parse_args()                # Aquí es donde argparse realmente mira lo que se escribió en la terminal. Por ejemplo, si ejecutas: python3 configurador.py --puerto /dev/ttyUSB1 --baudios 115200 
                                                        # parse_args() analiza eso y crea aproximadamente: argumentos.puerto = "/dev/ttyUSB1" argumentos.baudios = 115200
    baudios = argumentos.baudios
    rutaPuerto, niPuertoActual = seleccionarPuerto(
        argumentos.puerto,
        baudios,
    )
    try:
        configurarGtAlIniciar(rutaPuerto, baudios)
    except (ErrorXBee, serial.SerialException, OSError, ValueError) as error:
        raise SystemExit(f"Error al configurar GT: {error}") from error

    # print(f"GT configurado en {GT_CONFIGURADOR_MS} ms.")

    # Las opciones 1, 2, 3 y 5 devuelven el NI local para actualizar el encabezado.
    while True:
        mostrarMenu(rutaPuerto, baudios, niPuertoActual)
        opcion = input("Seleccione una opción: ").strip()

        try:
            if opcion == "1":                                               # Leer configuración del XBee local
                niPuertoActual = operacionLeer(rutaPuerto, baudios) 

            elif opcion == "2":                                             # Configurar ID, AP, CE y NI
                niPuertoActual = operacionConfigurar(rutaPuerto, baudios)

            elif opcion == "3":                                             # Descubrir módulos Xbee
                niPuertoActual = operacionDescubrir(rutaPuerto, baudios)

            elif opcion == "4":                                             # Mostrar registro de módulos
                mostrarRegistro()

            elif opcion == "5":                                             # Configurar a distancia otro XBee
                niPuertoActual = operacionConfigurarRemoto(rutaPuerto, baudios)

            elif opcion == "6":                                             # Cambiar puerto serial
                rutaPuerto, niPuertoActual = seleccionarPuerto(
                    baudios=baudios,
                )
                configurarGtAlIniciar(rutaPuerto, baudios)
                print(f"GT configurado en {GT_CONFIGURADOR_MS} ms.")

            elif opcion == "0":                                             # Salir
                print("Programa finalizado.")
                break

            else:
                print("Opción no válida.")  

        except KeyboardInterrupt:
            print("\nOperación cancelada por el usuario.")

        except (ErrorXBee, serial.SerialException, OSError, ValueError) as error:
            # Se informa el fallo y se vuelve al menú; no se anuncia éxito.
            print(f"\nError: {error}")


if __name__ == "__main__":
    main()
