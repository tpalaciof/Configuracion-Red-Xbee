#!/usr/bin/env python3
"""
Configurador local de una red XBee-PRO 900HP DigiMesh sin XCTU.

El programa se comunica con un XBee conectado a la Raspberry Pi mediante
un adaptador CP2102 USB-UART. Permite:

1. Leer la configuración actual del XBee local.
2. Configurar ID, AP, CE y NI.
3. Guardar los cambios en la memoria no volátil mediante WR.
4. Verificar que los valores fueron aplicados correctamente.
5. Descubrir los XBee que ya pertenecen a la misma red mediante ND.
6. Guardar un registro local de los módulos configurados.

Configuración recomendada para este proyecto:

    Raspberry: AP = 1, CE = 1  (Indirect Msg Coordinator)
    Sensores:  AP = 0, CE = 0  (Standard Router)

Requisito:

    python3 -m pip install pyserial
"""

import argparse
import json
import time
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
TIEMPO_RESPUESTA_AT_S = 2.0
TIEMPO_RESPUESTA_API_S = 2.0
TIEMPO_GUARDA_S = 1.100
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


# *********************** FUNCIONES BÁSICAS ********************** #

def fechaHoraActual():
    return datetime.now(ZONA_HORARIA).isoformat(timespec="seconds")


def enteroABytes(valor):
    cantidadBytes = max(1, (valor.bit_length() + 7) // 8) #El +7 convierte cualquier sobrante de 1 a 7 bits en un byte adicional.
    return valor.to_bytes(cantidadBytes, byteorder="big")


def bytesAEntero(datos):
    if not datos:
        return 0

    return int.from_bytes(datos, byteorder="big")


def textoHexadecimalAEntero(texto):
    texto = texto.strip() # elimina espacios y saltos de línea sobrantes al principio y al final

    if not texto:
        return 0

    return int(texto, 16) # print(int("FF", 16)) ---> 255


def descripcionModo(modo):
    if modo == MODO_COMANDO:
        return "transparente, utilizando comandos AT"

    if modo == MODO_API_1:
        return "API 1, sin escapes"

    return "API 2, con escapes"


def pedirConfirmacion(mensaje):
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


# *********************** TRAMAS API XBEE ************************ #

def escaparDatosApi(datos):
    resultado = bytearray()

    for byte in datos:
        if byte in (0x7E, 0x7D, 0x11, 0x13):
            resultado.append(0x7D)
            resultado.append(byte ^ 0x20)
        else:
            resultado.append(byte)

    return bytes(resultado)


def crearTramaApi(datosTrama, modo):
    longitud = len(datosTrama).to_bytes(2, byteorder="big")
    checksum = bytes([(0xFF - sum(datosTrama)) & 0xFF])
    contenido = longitud + datosTrama + checksum

    if modo == MODO_API_2:
        contenido = escaparDatosApi(contenido)

    return b"\x7E" + contenido


def leerByteApi(puerto, modo, tiempoFinal):
    while time.monotonic() < tiempoFinal:
        dato = puerto.read(1)

        if not dato:
            continue

        byte = dato[0]

        if modo == MODO_API_2 and byte == 0x7D:
            siguiente = puerto.read(1)

            if not siguiente:
                raise ErrorXBee("se recibió un escape API incompleto")

            return siguiente[0] ^ 0x20

        return byte

    raise TimeoutError


def leerTramaApi(puerto, modo, tiempoEspera=TIEMPO_RESPUESTA_API_S):
    tiempoFinal = time.monotonic() + tiempoEspera

    # Busca el delimitador inicial. Este byte nunca se escapa.
    while time.monotonic() < tiempoFinal:
        dato = puerto.read(1)

        if dato == b"\x7E":
            break
    else:
        raise TimeoutError

    byteLongitudAlto = leerByteApi(puerto, modo, tiempoFinal)
    byteLongitudBajo = leerByteApi(puerto, modo, tiempoFinal)
    longitud = (byteLongitudAlto << 8) | byteLongitudBajo

    datosTrama = bytearray()

    for _ in range(longitud):
        datosTrama.append(leerByteApi(puerto, modo, tiempoFinal))

    checksum = leerByteApi(puerto, modo, tiempoFinal)

    if ((sum(datosTrama) + checksum) & 0xFF) != 0xFF:
        raise ErrorXBee("se recibió una trama API con checksum incorrecto")

    return bytes(datosTrama)


def siguienteIdTrama():
    siguienteIdTrama.valor = (siguienteIdTrama.valor % 255) + 1
    return siguienteIdTrama.valor


siguienteIdTrama.valor = 0


def enviarComandoApi(
    puerto,
    modo,
    comando,
    parametro=None,
    tiempoEspera=TIEMPO_RESPUESTA_API_S,
):
    identificador = siguienteIdTrama()
    datosTrama = bytearray([0x08, identificador])
    datosTrama.extend(comando.encode("ascii"))

    if parametro is not None:
        datosTrama.extend(parametro)

    puerto.reset_input_buffer()
    puerto.write(crearTramaApi(bytes(datosTrama), modo))
    puerto.flush()

    tiempoFinal = time.monotonic() + tiempoEspera

    while time.monotonic() < tiempoFinal:
        restante = tiempoFinal - time.monotonic()

        try:
            respuesta = leerTramaApi(puerto, modo, restante)
        except TimeoutError:
            break

        if len(respuesta) < 5 or respuesta[0] != 0x88:
            continue

        if respuesta[1] != identificador:
            continue

        comandoRespuesta = respuesta[2:4].decode("ascii", errors="replace")

        if comandoRespuesta != comando:
            continue

        estado = respuesta[4]
        datos = respuesta[5:]

        if estado != 0:
            descripcion = NOMBRES_ESTADO_AT.get(
                estado,
                f"estado desconocido {estado}",
            )
            raise ErrorXBee(f"AT{comando}: {descripcion}")

        return datos

    raise ErrorXBee(f"el XBee no respondió al comando API AT{comando}")


# *********************** MODO COMANDO AT ************************ #

def leerRespuestaTexto(puerto, tiempoEspera=TIEMPO_RESPUESTA_AT_S): # Si no se especifica el segundo parámetro de tiempo, se usará el valor predeterminado de TIEMPO_RESPUESTA_AT_S = 2.0 segundos.
    tiempoFinal = time.monotonic() + tiempoEspera   # time.monotonic() = tiempo inicial en el que inicia la función. En base a esto se calcula el tiempo final que se necesita para salir del while. tiempoFinal es un valor fijo. 
    respuesta = bytearray()                         # Contenedor vacío para almacenar bytes
    recibioRespuesta = False

    while time.monotonic() < tiempoFinal: # time.monotonic() inicia como el tiempo inicial, pero dentro del while se va actualizando hasta superar el tiempo final. Es decir, cuando pase el tiempo de espera. 
        dato = puerto.read(1)             # Intenta leer un byte del puerto serial  

        if not dato:
            continue

        if dato in (b"\r", b"\n"):                  # Pregunta si el byte recibido es \r o \n
            if dato == b"\r" or recibioRespuesta:   # Si el dato es \r, entonces retorna la respuesta. Si el dato es \n y ya se recibió una respuesta anteriormente (recibioRespuesta == True), también retorna la respuesta. 
                return respuesta.decode(
                    "ascii",
                    errors="replace",               # Si aparece algún byte que no pueda interpretarse como ASCII, no se provoca una excepción y se reemplaza por un carácter especial.
                ).strip()                           # elimina espacios y saltos de línea sobrantes al principio y al final.

            continue                                # Sirve para ignorar un salto de línea inicial

        respuesta.extend(dato)                      # Añade la lista de elementos (byte) al final de la lista actual
        recibioRespuesta = True

    return None


def entrarModoComando(puerto):
    puerto.reset_input_buffer()     # borra todos los bytes que estuvieran pendientes de lectura.
    puerto.reset_output_buffer()    # borra datos pendientes de transmisión que todavía estuvieran en el buffer de salida

    time.sleep(TIEMPO_GUARDA_S) # Por defecto, el Xbee exige un tiempo de silencio de por lo menos 1 segundo antes y después de enviar +++
    puerto.write(b"+++")
    puerto.flush()              # Hace que Python espere hasta que los datos pendientes de escritura hayan sido enviados al sistema serial
    time.sleep(TIEMPO_GUARDA_S)

    respuesta = leerRespuestaTexto(puerto, 0.6) # Se espera un máximo de 0.6 segundos para recibir la respuesta del XBee. Si no llega nada, se retorna None. Si llega algo, se retorna la respuesta decodificada y sin espacios ni saltos de línea al principio y al final.
    return respuesta == "OK"                    # Según el manual, el equipo debe responder con OK\r una vez entre en el modo comnando
                                                # Si la respuesta es "OK", significa que se logró entrar en modo comando. Si no, significa que no se pudo entrar en modo comando y se retorna False.
                                                


def enviarComandoTexto(puerto, comando, parametro=None):
    texto = f"AT{comando}"

    if parametro is not None:
        texto += parametro

    puerto.reset_input_buffer()     # borra todos los bytes que estuvieran pendientes de lectura.
    puerto.write((texto + "\r").encode("ascii"))
    puerto.flush()

    respuesta = leerRespuestaTexto(puerto)

    if respuesta == "ERROR" or respuesta is None:
        if respuesta == "ERROR":
            raise ErrorXBee(f"el XBee rechazó el comando {texto}")

        raise ErrorXBee(f"el XBee no respondió al comando {texto}")

    return respuesta


def salirModoComando(puerto):
    try:
        enviarComandoTexto(puerto, "CN")
    except ErrorXBee:
        # Puede ocurrir si el módulo ya salió por tiempo de espera.
        pass


def identificarNiEnPuerto(rutaPuerto, baudios):
    """Lee ATNI sin modificar la configuración del XBee conectado."""

    try:
        with abrirPuerto(rutaPuerto, baudios) as puerto:
            if not entrarModoComando(puerto):
                return None

            try:
                ni = enviarComandoTexto(puerto, "NI").strip()
            finally:
                salirModoComando(puerto)

            return ni or None

    except (ErrorXBee, serial.SerialException, OSError, ValueError):
        # El puerto puede pertenecer a otro dispositivo o estar siendo usado.
        return None


# ******************** DETECCIÓN Y CONSULTAS ********************* #

def detectarModoXBee(puerto):
    print("Detectando el modo serial actual del XBee...")

    if entrarModoComando(puerto):
        # En el XBee-PRO 900HP también es posible entrar con +++ cuando AP
        # está configurado en 1 o 2. Por eso, lograr entrar en Command Mode
        # no significa necesariamente que el valor de AP sea 0.
        try:
            respuestaAp = enviarComandoTexto(puerto, "AP")
            ap = textoHexadecimalAEntero(respuestaAp)
        except (ErrorXBee, ValueError):
            salirModoComando(puerto)
        else:
            if ap == 0:
                # Se conserva abierto Command Mode para las consultas AT que
                # realizará la operación que llamó esta función.
                return MODO_COMANDO

            # Para AP=1 o AP=2 hay que salir de Command Mode antes de enviar
            # tramas API por la UART.
            salirModoComando(puerto)
            time.sleep(0.100)

            if ap == 1:
                return MODO_API_1

            if ap == 2:
                return MODO_API_2

    # Este segundo método queda como respaldo por si +++ está deshabilitado,
    # cambia el tiempo de guarda o la secuencia no fue reconocida.
    # Primero se intenta una trama sin escapes. La consulta AP normalmente
    # tampoco contiene bytes reservados, por lo que puede ser aceptada por un
    # módulo AP=2; el valor de la respuesta confirma el modo real.
    for modoIntentado in (MODO_API_1, MODO_API_2):
        puerto.reset_input_buffer()

        try:
            valorAp = enviarComandoApi(puerto, modoIntentado, "AP")
        except (ErrorXBee, TimeoutError):
            continue

        ap = bytesAEntero(valorAp)

        if ap == 1:
            return MODO_API_1

        if ap == 2:
            return MODO_API_2

    raise ErrorXBee(
        "no fue posible comunicarse con el XBee. Compruebe TX/RX, GND, "
        "la alimentación de 3.3 V, el puerto seleccionado y los 9600 baudios"
    )


def consultarComando(puerto, modo, comando):
    if modo == MODO_COMANDO:
        respuesta = enviarComandoTexto(puerto, comando)

        if comando == "NI":
            return respuesta.encode("ascii", errors="replace")

        return enteroABytes(textoHexadecimalAEntero(respuesta))

    return enviarComandoApi(puerto, modo, comando)


def leerConfiguracion(puerto, modo):
    configuracion = {}

    for comando in COMANDOS_LECTURA_OBLIGATORIOS:
        configuracion[comando] = consultarComando(puerto, modo, comando)

    for comando in COMANDOS_LECTURA_ADICIONALES:
        try:
            configuracion[comando] = consultarComando(puerto, modo, comando)
        except ErrorXBee:
            configuracion[comando] = None

    configuracion["ID"] = bytesAEntero(configuracion["ID"])
    configuracion["AP"] = bytesAEntero(configuracion["AP"])
    configuracion["CE"] = bytesAEntero(configuracion["CE"])
    configuracion["NI"] = configuracion["NI"].decode(
        "ascii",
        errors="replace",
    ).strip()
    configuracion["SH"] = bytesAEntero(configuracion["SH"])
    configuracion["SL"] = bytesAEntero(configuracion["SL"])

    for comando in COMANDOS_LECTURA_ADICIONALES:
        datos = configuracion[comando]

        if datos is not None:
            configuracion[comando] = bytesAEntero(datos)

    configuracion["MAC"] = (
        f"{configuracion['SH']:08X}{configuracion['SL']:08X}"
    )

    return configuracion


def mostrarConfiguracion(configuracion, modoDetectado):
    ap = configuracion["AP"]
    ce = configuracion["CE"]

    print()
    print("Configuración actual del XBee")
    print("-" * 46)
    print(f"MAC SH:SL : {configuracion['MAC']}")
    print(f"NI        : {configuracion['NI'] or '(vacío)'}")
    print(f"ID        : 0x{configuracion['ID']:04X}")
    print(f"AP        : {ap} - {NOMBRES_AP.get(ap, 'valor desconocido')}")
    print(f"CE        : {ce} - {NOMBRES_CE.get(ce, 'valor desconocido')}")
    print(f"Interfaz  : {descripcionModo(modoDetectado)}")

    if configuracion.get("BD") is not None:
        bd = configuracion["BD"]
        velocidad = BAUDIOS_POR_BD.get(bd)

        if velocidad:
            print(f"BD        : {bd:X} - {velocidad} baudios")
        else:
            print(f"BD        : 0x{bd:X}")

    for comando in ("VR", "HP", "CM"):
        valor = configuracion.get(comando)

        if valor is not None:
            print(f"{comando:<10}: 0x{valor:X}")

    if configuracion.get("DH") is not None and configuracion.get("DL") is not None:
        print(
            "DH:DL     : "
            f"{configuracion['DH']:08X}{configuracion['DL']:08X}"
        )

    print("-" * 46)


# ******************** ENTRADA DE PARÁMETROS ********************* #

def pedirId(valorActual):
    while True:
        respuesta = input(
            f"ID de red en hexadecimal [0x{valorActual:04X}]: "
        ).strip()

        if not respuesta:
            return valorActual

        if respuesta.lower().startswith("0x"):
            respuesta = respuesta[2:]

        try:
            valor = int(respuesta, 16)
        except ValueError:
            print("El ID debe ser un número hexadecimal.")
            continue

        if 0 <= valor <= 0x7FFF:
            return valor

        print("Para el XBee-PRO 900HP, ID debe estar entre 0x0000 y 0x7FFF.")


def pedirAp(valorActual):
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
    print()
    print("CE - Routing/Messaging Mode")
    print("  0. Standard Router")
    print("  1. Indirect Msg Coordinator")
    print("Recomendación: Coordinador = 1; Router = 0.")

    valorPredeterminado = valorActual if valorActual in (0, 1) else 0

    while True:
        respuesta = input(f"Seleccione CE [{valorPredeterminado}]: ").strip()

        if not respuesta:
            return valorPredeterminado

        if respuesta in ("0", "1"):
            return int(respuesta)

        print("En este proyecto CE solamente puede ser 0 o 1.")


def pedirNi(valorActual):
    while True:
        respuesta = input(f"NI - Nombre del nodo [{valorActual}]: ").strip()

        if not respuesta:
            respuesta = valorActual

        try:
            datos = respuesta.encode("ascii")
        except UnicodeEncodeError:
            print("NI solamente puede contener caracteres ASCII.")
            continue

        if not datos:
            print("NI no puede quedar vacío en este proyecto.")
            continue

        if len(datos) > 20:
            print("NI puede tener como máximo 20 caracteres ASCII.")
            continue

        if "," in respuesta or "\r" in respuesta or "\n" in respuesta:
            print("NI no puede contener comas ni saltos de línea.")
            continue

        return respuesta


def pedirNuevaConfiguracion(configuracionActual):
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

def establecerComando(puerto, modo, comando, valor):
    if comando == "NI":
        parametroApi = valor.encode("ascii")
        parametroTexto = valor
    else:
        parametroApi = enteroABytes(valor)
        parametroTexto = f"{valor:X}"

    if modo == MODO_COMANDO:
        respuesta = enviarComandoTexto(
            puerto,
            comando,
            parametroTexto,
        )

        if respuesta != "OK":
            raise ErrorXBee(
                f"respuesta inesperada al configurar AT{comando}: {respuesta}"
            )
    else:
        enviarComandoApi(puerto, modo, comando, parametroApi)


def guardarCambios(puerto, modo):
    if modo == MODO_COMANDO:
        respuesta = enviarComandoTexto(puerto, "WR")

        if respuesta != "OK":
            raise ErrorXBee(f"ATWR respondió: {respuesta}")
    else:
        enviarComandoApi(puerto, modo, "WR")

    # WR escribe en memoria no volátil. Se deja una pequeña pausa antes
    # de transmitir el siguiente comando.
    time.sleep(0.250)


def configurarEnModoComando(puerto, nuevaConfiguracion):
    establecerComando(puerto, MODO_COMANDO, "ID", nuevaConfiguracion["ID"])
    establecerComando(puerto, MODO_COMANDO, "CE", nuevaConfiguracion["CE"])
    establecerComando(puerto, MODO_COMANDO, "NI", nuevaConfiguracion["NI"])
    establecerComando(puerto, MODO_COMANDO, "AP", nuevaConfiguracion["AP"])
    guardarCambios(puerto, MODO_COMANDO)
    salirModoComando(puerto)


def configurarEnModoApi(puerto, modoActual, nuevaConfiguracion):
    # Se escriben y guardan primero los parámetros que no cambian la interfaz
    # serial. Así quedan protegidos incluso si hubiera un problema al cambiar AP.
    establecerComando(puerto, modoActual, "ID", nuevaConfiguracion["ID"])
    establecerComando(puerto, modoActual, "CE", nuevaConfiguracion["CE"])
    establecerComando(puerto, modoActual, "NI", nuevaConfiguracion["NI"])
    guardarCambios(puerto, modoActual)

    apActual = 1 if modoActual == MODO_API_1 else 2
    apNuevo = nuevaConfiguracion["AP"]

    if apNuevo == apActual:
        return

    # AP se cambia al final porque modifica la forma en que el módulo interpreta
    # los bytes de la UART. Después se detecta el nuevo modo y se guarda AP.
    establecerComando(puerto, modoActual, "AP", apNuevo)
    time.sleep(0.400)

    modoNuevo = detectarModoXBee(puerto)

    if apNuevo == 0 and modoNuevo != MODO_COMANDO:
        raise ErrorXBee("el XBee no cambió al modo transparente solicitado")

    if apNuevo == 1 and modoNuevo != MODO_API_1:
        raise ErrorXBee("el XBee no cambió a API 1")

    if apNuevo == 2 and modoNuevo != MODO_API_2:
        raise ErrorXBee("el XBee no cambió a API 2")

    guardarCambios(puerto, modoNuevo)

    if modoNuevo == MODO_COMANDO:
        salirModoComando(puerto)


def aplicarConfiguracion(puerto, modoActual, nuevaConfiguracion):
    if modoActual == MODO_COMANDO:
        configurarEnModoComando(puerto, nuevaConfiguracion)
    else:
        configurarEnModoApi(puerto, modoActual, nuevaConfiguracion)


def comprobarConfiguracion(configuracionLeida, configuracionEsperada):
    diferencias = []

    for comando in ("ID", "AP", "CE", "NI"):
        if configuracionLeida[comando] != configuracionEsperada[comando]:
            diferencias.append(
                f"{comando}: esperado {configuracionEsperada[comando]!r}, "
                f"leído {configuracionLeida[comando]!r}"
            )

    return diferencias


# *************************** REGISTRO **************************** #

def cargarRegistro():
    if not RUTA_REGISTRO.exists():
        return {
            "version": 1,
            "actualizado": None,
            "modulos": [],
            "ultimo_descubrimiento": None,
        }

    try:
        with RUTA_REGISTRO.open("r", encoding="utf-8") as archivo:
            registro = json.load(archivo)
    except (OSError, json.JSONDecodeError) as error:
        raise ErrorXBee(
            f"no fue posible leer {RUTA_REGISTRO.name}: {error}"
        ) from error

    registro.setdefault("version", 1)
    registro.setdefault("actualizado", None)
    registro.setdefault("modulos", [])
    registro.setdefault("ultimo_descubrimiento", None)
    return registro


def escribirRegistro(registro):
    registro["actualizado"] = fechaHoraActual()
    rutaTemporal = RUTA_REGISTRO.with_suffix(".json.tmp")

    try:
        with rutaTemporal.open("w", encoding="utf-8") as archivo:
            json.dump(registro, archivo, indent=4, ensure_ascii=False)
            archivo.write("\n")

        rutaTemporal.replace(RUTA_REGISTRO)
    except OSError as error:
        raise ErrorXBee(
            f"no fue posible escribir {RUTA_REGISTRO.name}: {error}"
        ) from error


def guardarModuloEnRegistro(configuracion, rutaPuerto):
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
    }

    for indice, anterior in enumerate(registro["modulos"]):
        if anterior.get("mac") == mac:
            registro["modulos"][indice] = modulo
            break
    else:
        registro["modulos"].append(modulo)

    escribirRegistro(registro)


def mostrarRegistro():
    registro = cargarRegistro()
    modulos = registro["modulos"]

    print()
    print(f"Registro: {RUTA_REGISTRO}")

    if not modulos:
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


# ********************* DESCUBRIMIENTO DE RED ******************** #

def analizarRespuestaNd(datos, opcionesNo):
    if len(datos) < 16:
        raise ErrorXBee("se recibió una respuesta ND demasiado corta")

    sh = int.from_bytes(datos[0:4], byteorder="big")
    sl = int.from_bytes(datos[4:8], byteorder="big")
    rssi = -datos[8]

    try:
        finNi = datos.index(0x00, 9)
    except ValueError as error:
        raise ErrorXBee("la respuesta ND no contiene el final de NI") from error

    ni = datos[9:finNi].decode("ascii", errors="replace")
    posicion = finNi + 1

    if len(datos) < posicion + 6:
        raise ErrorXBee("la respuesta ND no contiene todos los campos básicos")

    tipoNodo = datos[posicion]
    estado = datos[posicion + 1]
    profileId = int.from_bytes(datos[posicion + 2:posicion + 4], "big")
    manufacturerId = int.from_bytes(datos[posicion + 4:posicion + 6], "big")
    posicion += 6

    digiDeviceType = None
    rssiUltimoSalto = None

    if opcionesNo & 0x01 and len(datos) >= posicion + 4:
        digiDeviceType = int.from_bytes(datos[posicion:posicion + 4], "big")
        posicion += 4

    if opcionesNo & 0x04 and len(datos) > posicion:
        rssiUltimoSalto = -datos[posicion]

    return {
        "mac": f"{sh:08X}{sl:08X}",
        "sh": f"{sh:08X}",
        "sl": f"{sl:08X}",
        "ni": ni,
        "rssi_dbm": rssi,
        "tipo_nodo": tipoNodo,
        "tipo_nodo_texto": NOMBRES_TIPO_NODO.get(
            tipoNodo,
            f"Desconocido ({tipoNodo})",
        ),
        "estado": estado,
        "profile_id": f"0x{profileId:04X}",
        "manufacturer_id": f"0x{manufacturerId:04X}",
        "digi_device_type": (
            f"0x{digiDeviceType:08X}"
            if digiDeviceType is not None
            else None
        ),
        "rssi_ultimo_salto_dbm": rssiUltimoSalto,
    }


def descubrirNodosApi(puerto, modo):
    opcionesNo = bytesAEntero(enviarComandoApi(puerto, modo, "NO"))
    tiempoNt = bytesAEntero(enviarComandoApi(puerto, modo, "NT")) * 0.100
    tiempoEspera = min(
        tiempoNt + 2.0,
        TIEMPO_MAXIMO_DESCUBRIMIENTO_S,
    )

    print()
    print(f"Ejecutando ND. Tiempo máximo de espera: {tiempoEspera:.1f} s...")

    if tiempoNt + 2.0 > TIEMPO_MAXIMO_DESCUBRIMIENTO_S:
        print(
            "Aviso: NT solicita una espera mayor; esta versión la limita a "
            f"{TIEMPO_MAXIMO_DESCUBRIMIENTO_S:.0f} s."
        )

    identificador = siguienteIdTrama()
    datosTrama = bytes([0x08, identificador]) + b"ND"

    puerto.reset_input_buffer()
    puerto.write(crearTramaApi(datosTrama, modo))
    puerto.flush()

    tiempoFinal = time.monotonic() + tiempoEspera
    nodosPorMac = {}

    while time.monotonic() < tiempoFinal:
        restante = tiempoFinal - time.monotonic()

        try:
            respuesta = leerTramaApi(puerto, modo, restante)
        except TimeoutError:
            break

        if len(respuesta) < 5 or respuesta[0] != 0x88:
            continue

        if respuesta[1] != identificador or respuesta[2:4] != b"ND":
            continue

        estado = respuesta[4]

        if estado != 0:
            descripcion = NOMBRES_ESTADO_AT.get(estado, str(estado))
            raise ErrorXBee(f"la búsqueda ND terminó con estado {descripcion}")

        datos = respuesta[5:]

        # Una respuesta sin datos marca el final del descubrimiento.
        if not datos:
            break

        try:
            nodo = analizarRespuestaNd(datos, opcionesNo)
        except ErrorXBee as error:
            print(f"Aviso: se ignoró una respuesta ND: {error}")
            continue

        nodosPorMac[nodo["mac"]] = nodo
        print(
            f"  Encontrado: {nodo['ni'] or '(sin NI)'} | "
            f"{nodo['mac']} | {nodo['rssi_dbm']} dBm"
        )

    return list(nodosPorMac.values())


def mostrarNodosDescubiertos(nodos):
    print()

    if not nodos:
        print("No se encontraron módulos remotos en la misma red.")
        return

    print(f"Módulos encontrados: {len(nodos)}")
    print("-" * 74)

    for indice, nodo in enumerate(nodos, start=1):
        print(f"{indice}. NI: {nodo['ni'] or '(sin NI)'}")
        print(f"   MAC: {nodo['mac']}")
        print(f"   Tipo: {nodo['tipo_nodo_texto']}")
        print(f"   RSSI informado por ND: {nodo['rssi_dbm']} dBm")

        if nodo["rssi_ultimo_salto_dbm"] is not None:
            print(
                "   RSSI del último salto: "
                f"{nodo['rssi_ultimo_salto_dbm']} dBm"
            )

    print("-" * 74)


def guardarDescubrimiento(configuracionLocal, nodos):
    registro = cargarRegistro()
    registro["ultimo_descubrimiento"] = {
        "fecha": fechaHoraActual(),
        "id_red": f"0x{configuracionLocal['ID']:04X}",
        "coordinador_local": configuracionLocal["MAC"],
        "nodos": nodos,
    }
    escribirRegistro(registro)


# ********************** OPERACIONES DEL MENÚ ******************** #

def operacionLeer(rutaPuerto, baudios):
    with abrirPuerto(rutaPuerto, baudios) as puerto:
        modo = detectarModoXBee(puerto)
        configuracion = leerConfiguracion(puerto, modo)
        mostrarConfiguracion(configuracion, modo)

        if modo == MODO_COMANDO:
            salirModoComando(puerto)

    return configuracion["NI"] or None


def operacionConfigurar(rutaPuerto, baudios):
    # Primera conexión: leer la configuración y salir de Command Mode antes
    # de pedir datos, para que el XBee no venza su tiempo de espera de 10 s.
    with abrirPuerto(rutaPuerto, baudios) as puerto:
        modoInicial = detectarModoXBee(puerto)
        configuracionActual = leerConfiguracion(puerto, modoInicial)
        mostrarConfiguracion(configuracionActual, modoInicial)

        if modoInicial == MODO_COMANDO:
            salirModoComando(puerto)

    nuevaConfiguracion = pedirNuevaConfiguracion(configuracionActual)

    if not pedirConfirmacion("¿Desea escribir estos valores en el XBee?"):
        print("Configuración cancelada. No se modificó el XBee.")
        return configuracionActual["NI"] or None

    print()
    print("Escribiendo configuración...")

    with abrirPuerto(rutaPuerto, baudios) as puerto:
        modoActual = detectarModoXBee(puerto)
        aplicarConfiguracion(puerto, modoActual, nuevaConfiguracion)

    # Se abre nuevamente el puerto para comprobar el modo activo y los valores.
    time.sleep(0.500)

    with abrirPuerto(rutaPuerto, baudios) as puerto:
        modoFinal = detectarModoXBee(puerto)
        configuracionFinal = leerConfiguracion(puerto, modoFinal)
        diferencias = comprobarConfiguracion(
            configuracionFinal,
            nuevaConfiguracion,
        )
        mostrarConfiguracion(configuracionFinal, modoFinal)

        if modoFinal == MODO_COMANDO:
            salirModoComando(puerto)

    if diferencias:
        print("La verificación encontró diferencias:")

        for diferencia in diferencias:
            print(f"  - {diferencia}")

        raise ErrorXBee("la configuración no quedó verificada")

    guardarModuloEnRegistro(configuracionFinal, rutaPuerto)

    print("Configuración guardada y verificada correctamente.")
    print(f"Registro actualizado: {RUTA_REGISTRO.name}")
    return configuracionFinal["NI"] or None


def operacionDescubrir(rutaPuerto, baudios):
    with abrirPuerto(rutaPuerto, baudios) as puerto:
        modo = detectarModoXBee(puerto)

        if modo == MODO_COMANDO:
            configuracion = leerConfiguracion(puerto, modo)
            salirModoComando(puerto)

            raise ErrorXBee(
                "el XBee local está en AP=0. Para el descubrimiento estructurado "
                "mediante tramas 0x08/0x88, configure el XBee de la Raspberry "
                "con AP=1 o AP=2"
            )

        configuracion = leerConfiguracion(puerto, modo)
        mostrarConfiguracion(configuracion, modo)

        print(
            "ND solamente encontrará módulos que ya compartan ID, HP, CM "
            "y una configuración de radio compatible."
        )

        nodos = descubrirNodosApi(puerto, modo)

    mostrarNodosDescubiertos(nodos)
    guardarDescubrimiento(configuracion, nodos)
    print(f"Resultado guardado en {RUTA_REGISTRO.name}.")
    return configuracion["NI"] or None


def mostrarMenu(rutaPuerto, baudios, niPuertoActual=None):
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
    print("  3. Descubrir módulos de la misma red con ND")
    print("  4. Mostrar registro de módulos")
    print("  5. Cambiar puerto serial")
    print("  0. Salir")


# ************************ PROGRAMA PRINCIPAL ********************* #

def main():
    analizador = argparse.ArgumentParser(
        description=(
            "Configura XBee-PRO 900HP DigiMesh por CP2102 sin utilizar XCTU."
        )
    )
    analizador.add_argument(
        "--puerto",
        help="Puerto serial, por ejemplo /dev/ttyUSB0",
    )
    analizador.add_argument(
        "--baudios",
        type=int,
        default=BAUDIOS,
        help=f"Velocidad serial actual del XBee; predeterminado {BAUDIOS}",
    )
    argumentos = analizador.parse_args()

    baudios = argumentos.baudios
    rutaPuerto, niPuertoActual = seleccionarPuerto(
        argumentos.puerto,
        baudios,
    )

    while True:
        mostrarMenu(rutaPuerto, baudios, niPuertoActual)
        opcion = input("Seleccione una opción: ").strip()

        try:
            if opcion == "1":
                niPuertoActual = operacionLeer(rutaPuerto, baudios)

            elif opcion == "2":
                niPuertoActual = operacionConfigurar(rutaPuerto, baudios)

            elif opcion == "3":
                niPuertoActual = operacionDescubrir(rutaPuerto, baudios)

            elif opcion == "4":
                mostrarRegistro()

            elif opcion == "5":
                rutaPuerto, niPuertoActual = seleccionarPuerto(
                    baudios=baudios,
                )

            elif opcion == "0":
                print("Programa finalizado.")
                break

            else:
                print("Opción no válida.")

        except KeyboardInterrupt:
            print("\nOperación cancelada por el usuario.")

        except (ErrorXBee, serial.SerialException, OSError, ValueError) as error:
            print(f"\nError: {error}")


if __name__ == "__main__":
    main()
